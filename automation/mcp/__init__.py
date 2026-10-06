"""MCP (Model Context Protocol) servers connected to the platform.

Servers are configured on the Settings page (stored as McpServer rows), and the
AI Chat can call their tools. See client.py (the protocol) and registry.py
(the live connections and the guard that keeps them off busy simulators).
"""
