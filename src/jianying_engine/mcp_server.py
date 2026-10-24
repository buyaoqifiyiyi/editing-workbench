"""Official MCP SDK stdio adapter; all execution goes through the engine command whitelist."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import os
from typing import Any
import anyio

from mcp.server import MCPServer
from pydantic import StrictInt

from . import api
from .jobs import JobManager


def create_server(state_dir=None):
    manager = JobManager(state_dir)

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            with anyio.CancelScope(shield=True):
                await manager.close()

    server = MCPServer('jianying-engine', lifespan=lifespan,
                       instructions='Local editing engine. Start returns a job ID, not completion. Poll job_status. '
                                    'Only succeeded means exit code 0; inspect engine output for evidence boundaries. '
                                    'Only music-edit analyze calls the explicitly configured multimodal service/model and sends selected media; '
                                    'other operations do not call models. Jobs do not survive this server session.')

    @server.tool(structured_output=True)
    async def describe_operations() -> dict[str, Any]:
        """Discover the explicit engine operations; no shell command or Python code can be supplied."""
        value = api.operations() if callable(api.operations) else api.operations
        return {'operations': value, 'guides': list(api.paths()['guides']),
                'transport': 'stdio', 'persistent_jobs': False}

    @server.tool(structured_output=True)
    async def read_guide(name: str) -> dict[str, Any]:
        """Read a named bundled guide through the core API; no arbitrary filesystem paths are accepted."""
        try:
            text = await asyncio.to_thread(api.read_guide, name)
            return {'name': name, 'text': text[:131072], 'truncated': len(text) > 131072}
        except (ValueError, OSError) as exc:
            return {'name': name, 'error': str(exc)}

    @server.tool(structured_output=True)
    async def start_job(operation: str, arguments: list[str], timeout_seconds: float = 1800) -> dict[str, Any]:
        """Start one whitelisted operation with CLI argument tokens; returns quickly with a job ID."""
        try:
            # Once a subprocess launch begins, retain ownership even if the client cancels this quick request.
            with anyio.CancelScope(shield=True):
                return await manager.start(operation, arguments, timeout_seconds)
        except (ValueError, OSError) as exc:
            return {'state': 'rejected', 'error': str(exc), 'job_id': None, 'returncode': None}

    @server.tool(structured_output=True)
    async def job_status(job_id: str) -> dict[str, Any]:
        """Return lifecycle state, exit code, bounded stdout/stderr and external log locations."""
        try:
            return await manager.status(job_id)
        except ValueError as exc:
            return {'state': 'unknown', 'error': str(exc), 'job_id': job_id, 'returncode': None}

    @server.tool(structured_output=True)
    async def read_job_output(job_id: str, stream: str = 'stdout', offset: StrictInt = 0,
                              limit: StrictInt = 32768) -> dict[str, Any]:
        """Page a finished job's retained stdout/stderr by character offset; truncated logs are not complete JSON."""
        try:
            return await manager.read_output(job_id, stream, offset, limit)
        except (ValueError, OSError) as exc:
            return {'state': 'rejected', 'error': str(exc), 'job_id': job_id}

    @server.tool(structured_output=True)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Stop only this job's owned process tree and wait for its process and output readers to finish."""
        try:
            return await manager.cancel(job_id)
        except ValueError as exc:
            return {'state': 'unknown', 'error': str(exc), 'job_id': job_id, 'returncode': None}

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=os.environ.get('JIANYING_ENGINE_STATE_DIR'))
    args = parser.parse_args(argv)
    create_server(args.state_dir).run(transport='stdio')


if __name__ == '__main__':
    main()
