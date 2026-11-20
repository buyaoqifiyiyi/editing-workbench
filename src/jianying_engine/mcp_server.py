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
                                    'The music-edit analyze and review subcommands call the configured multimodal service/model and send selected media; '
                                    'other operations do not call models. Jobs do not survive this server session.')

    @server.tool(structured_output=True)
    async def describe_operations(operation: str | None = None, include_backends: bool = False) -> dict[str, Any]:
        """Discover explicit engine operations, structured parameter schemas, guides, job limits and optional backend availability."""
        value = api.operations() if callable(api.operations) else api.operations
        job_limits = {
            'concurrent_limit': manager.concurrent_limit,
            'retained_terminal_limit': manager.job_limit,
            'eviction': 'lru_terminal_only',
            'logs_persisted_on_disk': True,
        }
        if operation is not None:
            try:
                schema = api.operation_schemas(operation)
            except ValueError as exc:
                return {'operation': operation, 'error': str(exc), 'job_limits': job_limits}
            result = {
                'operation': operation,
                'description': value[operation],
                'schema': schema,
                'guides': list(api.paths()['guides']),
                'transport': 'stdio',
                'persistent_jobs': False,
                'job_limits': job_limits,
            }
        else:
            result = {
                'operations': value,
                'operation_schemas': api.operation_schemas(),
                'guides': list(api.paths()['guides']),
                'transport': 'stdio',
                'persistent_jobs': False,
                'job_limits': job_limits,
            }
        if include_backends:
            result['backends'] = await asyncio.to_thread(api.backends)
        return result

    @server.tool(structured_output=True)
    async def read_guide(name: str) -> dict[str, Any]:
        """Read a named bundled guide through the core API; no arbitrary filesystem paths are accepted."""
        try:
            text = await asyncio.to_thread(api.read_guide, name)
            return {'name': name, 'text': text[:131072], 'truncated': len(text) > 131072}
        except (ValueError, OSError) as exc:
            return {'name': name, 'error': str(exc)}

    @server.tool(structured_output=True)
    async def search_knowledge(query: str, limit: StrictInt = 5) -> dict[str, Any]:
        """Search only bundled editing guides; no arbitrary paths, media or network."""
        try:
            return await asyncio.to_thread(api.search_knowledge, query, limit)
        except (ValueError, OSError) as exc:
            return {'query': query, 'results': [], 'error': str(exc)}

    @server.tool(structured_output=True)
    async def start_job(
        operation: str,
        arguments: list[str] | None = None,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 1800,
    ) -> dict[str, Any]:
        """Start one whitelisted operation using CLI tokens or structured params; returns quickly with a job ID."""
        try:
            if arguments is not None and params is not None:
                raise ValueError('Specify either arguments or params, not both')
            tokens = (api.arguments_from_params(operation, params) if params is not None
                      else ([] if arguments is None else arguments))
            # Once a subprocess launch begins, retain ownership even if the client cancels this quick request.
            with anyio.CancelScope(shield=True):
                return await manager.start(operation, tokens, timeout_seconds)
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
