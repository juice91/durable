"""MCP server registry (milestone 3, task 3.4): tables, header-secret storage and the connection test.

Tenants register remote MCP servers over Streamable HTTP only. URLs pass the outbound policy
(`eap_secrets.outbound`); header values are encrypted with the platform key (`eap_secrets`) and are
never returned. The connection test uses FastMCP's client (the one Pydantic AI's MCP support uses)
through the policy's guarded HTTP client; the API never imports pydantic_ai (layer rule).
"""
