class Audience:
    """What a secret authenticates at, as one string with its namespace, so a
    provider, a service, and an MCP server never share one."""

    @staticmethod
    def provider(provider_id: str) -> str:
        return f"provider:{provider_id}"

    @staticmethod
    def service(slug: str) -> str:
        return f"service:{slug}"

    @staticmethod
    def mcp(name: str) -> str:
        return f"mcp:{name}"
