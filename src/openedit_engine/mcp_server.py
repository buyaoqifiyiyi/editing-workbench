"""Official MCP SDK stdio adapter for the pure open-source editing engine."""
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

    server = MCPServer(
        'openedit-engine',
        lifespan=lifespan,
        instructions=(
            'Open-source video editing and VFX engine (FilmCraft + EffectCraft + FFmpeg + offline AI). '
            'For new or full-reedit creative work, first read_guide("planning") and consult search_knowledge to design both story/content progression and visual-audio editing decisions (including peer-reference study, proactive transition/connection creation, and contrast checks) before building timelines; for scoped edits, review only affected clips and adjacent transitions. '
            'start_job returns a job ID; poll job_status until terminal state and read_job_output for complete JSON. '
            'Only music-edit analyze/review call external configured multimodal endpoints (require task-specific authorization and exact video/audio version alignment); all core operations run locally. '
            'Before final delivery, verify preview frames and run audit (--contact-sheet) while keeping technical checks distinct from human continuous audiovisual review.'
        ),
    )

    @server.tool(structured_output=True)
    async def describe_operations(operation: str | None = None, include_backends: bool = False) -> dict[str, Any]:
        """Discover open-source engine operations, structured schemas, guides, job limits, and backend availability."""
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
        """Search bundled editing guides and technique libraries using offline BM25."""
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
            with anyio.CancelScope(shield=True):
                return await manager.start(operation, tokens, timeout_seconds)
        except (ValueError, OSError) as exc:
            return {'state': 'rejected', 'error': str(exc), 'job_id': None, 'returncode': None}

    @server.tool(structured_output=True)
    async def job_status(job_id: str) -> dict[str, Any]:
        """Return lifecycle state, exit code, bounded stdout/stderr, and log file locations."""
        try:
            return await manager.status(job_id)
        except ValueError as exc:
            return {'state': 'unknown', 'error': str(exc), 'job_id': job_id, 'returncode': None}

    @server.tool(structured_output=True)
    async def read_job_output(job_id: str, stream: str = 'stdout', offset: StrictInt = 0,
                              limit: StrictInt = 32768) -> dict[str, Any]:
        """Page a finished job's retained stdout/stderr by character offset."""
        try:
            return await manager.read_output(job_id, stream, offset, limit)
        except (ValueError, OSError) as exc:
            return {'state': 'rejected', 'error': str(exc), 'job_id': job_id}

    @server.tool(structured_output=True)
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Stop this job's owned process tree and wait for readers to finish."""
        try:
            return await manager.cancel(job_id)
        except ValueError as exc:
            return {'state': 'unknown', 'error': str(exc), 'job_id': job_id, 'returncode': None}

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', default=os.environ.get('OPENEDIT_ENGINE_STATE_DIR'))
    args = parser.parse_args(argv)
    create_server(args.state_dir).run(transport='stdio')


if __name__ == '__main__':
    main()
