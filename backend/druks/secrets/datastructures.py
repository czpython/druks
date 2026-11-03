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

    @staticmethod
    def oauth(*parts: object) -> str:
        """An access token minted from an OAuth connection's grant, at the path of
        ``parts``."""
        return f"oauth:{'/'.join(map(str, parts))}"

    @staticmethod
    def resource(*parts: object) -> str:
        """A token minted for a resource at a service, at the path of ``parts``."""
        return f"resource:{'/'.join(map(str, parts))}"
