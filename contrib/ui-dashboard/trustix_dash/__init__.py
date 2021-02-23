from trustix_dash.models import fields as trustix_fields
from trustix_dash.models import (
    Log,
    Evaluation,
    Derivation,
    DerivationAttr,
    DerivationOutput,
    DerivationOutputResult,
    DerivationRefRecursive,
    DerivationRefDirect,
    DerivationEval,
)
from tortoise.exceptions import DoesNotExist
from tortoise import transactions
import ijson  # type: ignore
from trustix_proto import trustix_pb2_grpc, trustix_pb2  # type: ignore
import grpc  # type: ignore
from async_lru import alru_cache  # type: ignore
import typing
import pynix
import aiofiles
import os.path
import asyncio
import json


TRUSTIX_RPC = "unix:../../sock"


SUPPORTED_SYSTEMS: typing.List[str] = [
    "aarch64-linux",
    "x86_64-linux",
    "x86_64-darwin",
]


channel = grpc.insecure_channel(TRUSTIX_RPC)
stub = trustix_pb2_grpc.TrustixCombinedRPCStub(channel)


@transactions.atomic()
async def index_eval(commit_sha: str):  # noqa: C901

    try:
        evaluation = await Evaluation.get(commit=commit_sha)
    except DoesNotExist:
        evaluation = await Evaluation.create(commit=commit_sha)

    refs: typing.Dict[str, typing.Set[str]] = {}

    def fake_drvs(*refs: str) -> typing.List[Derivation]:
        """
        HACK: Tortoise cant add m2m models by their id only
        Create an intermediate Derivation instance that tricks Tortoise
        """
        ret: typing.List[Derivation] = []
        for ref in refs:
            drv = Derivation(drv=ref)
            drv._saved_in_db = True
            ret.append(drv)
        return ret

    @alru_cache(maxsize=30_000)
    async def drv_read(drv_path: str) -> typing.Dict:
        async with aiofiles.open(drv_path) as f:  # type: ignore
            return pynix.drvparse(await f.read())

    async def gen_drvs(
        attr: typing.Optional[str], drv_path: str
    ) -> typing.AsyncGenerator[
        typing.Tuple[
            typing.Optional[str], typing.Dict, typing.Set[str], typing.Set[str], str
        ],
        None,
    ]:
        # TODO: Short circuit if exists, get refs, insert into refs cache and return

        drv = await drv_read(drv_path)

        # Direct dependencies
        refs_direct: typing.Set[str] = set(drv["inputDrvs"])

        # All dependencies (recursive, flattened)
        refs_all = refs_direct.copy()

        for input_ in drv["inputDrvs"]:
            if input_ not in refs:
                async for input_drv in gen_drvs(None, input_):
                    yield input_drv

            # If the input _still_ doesn't exist it means it's a fixed-output
            # and should be filtered out
            try:
                refs_all = refs_all | refs[input_]
            except KeyError:
                refs_direct.remove(input_)
                refs_all.remove(input_)

        # Filter fixed outputs
        if all("hashAlgo" in d for d in drv["outputs"].values()):
            return

        refs[drv_path] = refs_direct

        yield (attr, drv, refs_direct, refs_all, drv_path)

    async def gen_drvs_attrs() -> typing.AsyncGenerator[
        typing.Tuple[
            typing.Optional[str], typing.Dict, typing.Set[str], typing.Set[str], str
        ],
        None,
    ]:
        expr_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "hydra_eval"
        )

        env = os.environ.copy()
        try:
            del env["NIX_PATH"]
        except KeyError:
            pass

        proc = await asyncio.create_subprocess_exec(
            *[
                "hydra-eval-jobs",
                "-I",
                f"nixpkgs=https://github.com/NixOS/nixpkgs/archive/{commit_sha}.tar.gz",
                "-I",
                expr_dir,
                os.path.join(expr_dir, "outpaths.nix"),
                "--arg",
                "systems",
                json.dumps(SUPPORTED_SYSTEMS).replace(",", ""),
            ],
            env=env,
            stdout=asyncio.subprocess.PIPE,
        )
        async for attr, pkg in ijson.kvitems_async(proc.stdout, ""):
            if "error" in pkg:
                continue

            async for drv in gen_drvs(attr, pkg["drvPath"]):
                yield drv

    async for (attr, drv, refs_direct, refs_all, drv_path) in gen_drvs_attrs():
        if attr:
            print(f"Indexing {attr}")

        derivation_id = drv_path

        try:
            created = False
            d = await Derivation.get(drv=derivation_id)
        except DoesNotExist:
            created = True
            d = await Derivation(drv=derivation_id)
            d.system = drv["platform"]
            await d.save()

        async def get_or_create_attr():
            try:
                await DerivationAttr.get(derivation=d, attr=attr)
            except DoesNotExist:
                await DerivationAttr.create(derivation=d, attr=attr)

        coros: typing.List[typing.Coroutine] = []
        coros.append(DerivationEval.create(drv=d, eval=evaluation))
        if attr:
            coros.append(get_or_create_attr())

        if created:
            coros.append(
                DerivationRefDirect.bulk_create(
                    [
                        DerivationRefDirect(referrer=d, drv=ref_drv)
                        for ref_drv in fake_drvs(*refs_direct)
                    ]
                )
            )
            coros.append(
                DerivationRefRecursive.bulk_create(
                    [
                        DerivationRefRecursive(referrer=d, drv=ref_drv)
                        for ref_drv in fake_drvs(*refs_all)
                    ]
                )
            )

            async def get_or_create_output(
                output: str, store_path_meta: typing.Dict[str, str]
            ):
                store_path = store_path_meta["path"]
                input_hash = pynix.b32decode(store_path.split("/")[-1].split("-", 1)[0])
                try:
                    await DerivationOutput.get(input_hash=input_hash)
                except DoesNotExist:
                    await DerivationOutput.create(
                        derivation=d,
                        input_hash=input_hash,
                        output=output,
                        store_path=store_path,
                    )

            for output, store_path in drv["outputs"].items():
                coros.append(get_or_create_output(output, store_path))

        await asyncio.gather(*coros)


@transactions.atomic()
async def index_log(log, sth):
    start = max(0, log.tree_size - 1)
    finish = sth.TreeSize - 1

    if start >= finish:
        return

    chunks = list(range(log.tree_size, finish, 500))
    if chunks[-1] != finish:
        chunks.append(finish)

    start = chunks[0]
    for finish in chunks[1:]:
        # TODO: Async
        resp = stub.GetLogEntries(
            trustix_pb2.GetLogEntriesRequestNamed(
                LogName=log.name,
                Start=start,
                Finish=finish,
            )
        )
        print(f"Indexing logname={log.name}, start={start}, finish={finish}")

        async def get_or_create_result(leaf):
            try:
                await DerivationOutputResult.get(output_id=leaf.Key, log=log)
            except DoesNotExist:
                await DerivationOutputResult.create(
                    output_id=trustix_fields.BinaryField.encode_value(leaf.Key),
                    output_hash=leaf.ValueDigest,
                    log=log,
                )

        coros: typing.List[typing.Coroutine] = [
            get_or_create_result(leaf) for leaf in resp.Leaves
        ]
        await asyncio.gather(*coros)

        start = finish

    log.tree_size = sth.TreeSize
    await log.save()


async def index_logs():
    req = trustix_pb2.LogsRequest()
    resp = stub.Logs(req)  # TODO: Async

    for log_resp in resp.Logs:
        try:
            log = await Log.get(name=log_resp.Name)
        except DoesNotExist:
            log = await Log.create(name=log_resp.Name, tree_size=0)

        await index_log(log, log_resp.STH)
