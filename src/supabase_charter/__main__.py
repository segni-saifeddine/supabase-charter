"""Run the local MCP server. Bearer token comes from DB_AGENT_MCP_TOKEN."""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from supabase_charter.server import build_mcp, get_app


class BearerMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self.token = token

    async def dispatch(self, request, call_next):
        if not self.token:
            return JSONResponse(
                {"error": "DB_AGENT_MCP_TOKEN is not set"}, status_code=503
            )
        header = request.headers.get("authorization", "")
        if header != f"Bearer {self.token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def main() -> None:
    import uvicorn

    agent = get_app()
    mcp = build_mcp(agent)
    app = mcp.streamable_http_app()
    app.add_middleware(BearerMiddleware, token=agent.settings.mcp_token)
    uvicorn.run(app, host=agent.settings.host, port=agent.settings.port)


if __name__ == "__main__":
    main()
