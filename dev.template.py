import asyncio
import logging
import pathlib

from operator_core import Operator

logger = logging.getLogger(__name__)


class TestOperator(Operator):
    pass


async def main():
    operator = TestOperator(
        kube_config=pathlib.Path("/root/.kube/config"), logger=logger
    )
    await operator.run()


if __name__ == "__main__":
    logger = logging.getLogger("dev")
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(main())
