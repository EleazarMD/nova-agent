"""
Knowledge Graph MCP Integration for Nova Agent.

Provides tools for:
- Querying the AI Homelab Knowledge Graph
- Finding relationships between services, components, and data
- Context-aware answers using graph topology
- Tracking dependencies and integrations

MCP Server: workspace-books (port via stdio)
Endpoints:
- query_entity: Find entities by type, name, or properties
- query_relationship: Traverse relationships between entities
- search_graph: Full-text search across the graph
- get_entity_context: Get comprehensive context for an entity
"""

import json
import asyncio
from typing import Optional, List, Dict, Any
from dataclasses import dataclass
from loguru import logger


@dataclass
class KnowledgeGraphEntity:
    """Represents an entity from the Knowledge Graph."""
    id: str
    type: str
    name: str
    properties: Dict[str, Any]
    labels: List[str]


@dataclass
class KnowledgeGraphRelationship:
    """Represents a relationship between entities."""
    source_id: str
    target_id: str
    type: str
    properties: Dict[str, Any]


class KnowledgeGraphClient:
    """
    Client for the Knowledge Graph MCP server.
    
    Provides methods to query and traverse the AI Homelab Knowledge Graph
    for context-aware responses and dependency tracking.
    """
    
    def __init__(self, mcp_server_url: Optional[str] = None):
        # MCP uses stdio transport, so we interact via the MCP client
        # For now, we'll implement as a mock that can be replaced with real MCP calls
        self._mcp_available = False
        self._cache: Dict[str, Any] = {}
        
    async def _call_mcp_tool(self, tool_name: str, arguments: Dict) -> Dict:
        """
        Call an MCP tool.
        
        In production, this would use the MCP client to call the workspace-books server.
        For now, returns mock data for common queries.
        """
        # TODO: Replace with actual MCP client call when server is available
        logger.debug(f"MCP call: {tool_name}({arguments})")
        
        # Mock responses for development
        if tool_name == "query_entity":
            return self._mock_query_entity(arguments)
        elif tool_name == "query_relationship":
            return self._mock_query_relationship(arguments)
        elif tool_name == "search_graph":
            return self._mock_search_graph(arguments)
        elif tool_name == "get_entity_context":
            return self._mock_get_entity_context(arguments)
        
        return {"error": "Unknown tool", "results": []}
    
    def _mock_query_entity(self, args: Dict) -> Dict:
        """Mock entity query for development."""
        entity_type = args.get("type", "")
        name = args.get("name", "")
        
        # Service entities
        services = {
            "nova-agent": {
                "id": "nova-agent-001",
                "type": "Service",
                "name": "Nova Agent",
                "properties": {
                    "port": 18800,
                    "language": "Python",
                    "purpose": "Voice AI agent",
                    "status": "active",
                },
                "labels": ["Service", "Voice", "AI"],
            },
            "hermes-core": {
                "id": "hermes-core-001",
                "type": "Service",
                "name": "Hermes Core",
                "properties": {
                    "port": 8001,
                    "databases": ["Neo4j", "ChromaDB"],
                    "purpose": "Email and calendar management",
                    "status": "active",
                },
                "labels": ["Service", "Email", "Calendar"],
            },
            "ai-gateway": {
                "id": "ai-gateway-001",
                "type": "Service",
                "name": "AI Gateway",
                "properties": {
                    "port": 8777,
                    "purpose": "LLM routing and security",
                    "status": "active",
                },
                "labels": ["Service", "Gateway", "Security"],
            },
        }
        
        if name.lower() in services:
            return {"results": [services[name.lower()]]}
        
        return {"results": []}
    
    def _mock_query_relationship(self, args: Dict) -> Dict:
        """Mock relationship query for development."""
        entity_id = args.get("entity_id", "")
        relationship_type = args.get("relationship_type", "")
        
        # Mock: Nova Agent depends on AI Gateway and Hermes Core
        if "nova-agent" in entity_id:
            return {
                "results": [
                    {
                        "source_id": entity_id,
                        "target_id": "ai-gateway-001",
                        "type": "DEPENDS_ON",
                        "properties": {"critical": True},
                    },
                    {
                        "source_id": entity_id,
                        "target_id": "hermes-core-001",
                        "type": "USES",
                        "properties": {"purpose": "email/calendar queries"},
                    },
                ]
            }
        
        return {"results": []}
    
    def _mock_search_graph(self, args: Dict) -> Dict:
        """Mock graph search for development."""
        query = args.get("query", "").lower()
        
        results = []
        if "nova" in query or "agent" in query:
            results.append({
                "id": "nova-agent-001",
                "type": "Service",
                "name": "Nova Agent",
                "score": 0.95,
            })
        if "hermes" in query or "email" in query:
            results.append({
                "id": "hermes-core-001",
                "type": "Service",
                "name": "Hermes Core",
                "score": 0.92,
            })
        
        return {"results": results}
    
    def _mock_get_entity_context(self, args: Dict) -> Dict:
        """Mock entity context for development."""
        entity_id = args.get("entity_id", "")
        
        # Build comprehensive context
        if "nova-agent" in entity_id:
            return {
                "entity": {
                    "id": entity_id,
                    "type": "Service",
                    "name": "Nova Agent",
                },
                "direct_relationships": [
                    {"to": "AI Gateway", "type": "depends on"},
                    {"to": "Hermes Core", "type": "uses for email/calendar"},
                    {"to": "Tesla Relay", "type": "integrates with"},
                    {"to": "OpenClaw", "type": "delegates to"},
                ],
                "dependents": [
                    {"from": "Dashboard", "type": "uses"},
                ],
                "related_docs": [
                    {"title": "Hybrid Voice Agent Architecture", "chapter": 28},
                    {"title": "Zero-Wait Protocol", "section": "UI/UX"},
                ],
            }
        
        return {"entity": None, "context": "Entity not found"}
    
    # -------------------------------------------------------------------------
    # Public API Methods
    # -------------------------------------------------------------------------
    
    async def find_service(self, name: str) -> Optional[KnowledgeGraphEntity]:
        """Find a service by name."""
        result = await self._call_mcp_tool("query_entity", {
            "type": "Service",
            "name": name,
        })
        
        entities = result.get("results", [])
        if entities:
            e = entities[0]
            return KnowledgeGraphEntity(
                id=e["id"],
                type=e["type"],
                name=e["name"],
                properties=e.get("properties", {}),
                labels=e.get("labels", []),
            )
        return None
    
    async def get_service_dependencies(self, service_name: str) -> List[Dict]:
        """
        Get all services that a given service depends on.
        
        Useful for answering: "What does Nova Agent need to work?"
        """
        service = await self.find_service(service_name)
        if not service:
            return []
        
        result = await self._call_mcp_tool("query_relationship", {
            "entity_id": service.id,
            "relationship_type": "DEPENDS_ON",
            "direction": "outgoing",
        })
        
        relationships = result.get("results", [])
        return [
            {
                "service_id": r["target_id"],
                "relationship": r["type"],
                "properties": r.get("properties", {}),
            }
            for r in relationships
        ]
    
    async def get_service_integrations(self, service_name: str) -> List[Dict]:
        """
        Get all services integrated with a given service.
        
        Useful for answering: "What integrates with Hermes Core?"
        """
        service = await self.find_service(service_name)
        if not service:
            return []
        
        result = await self._call_mcp_tool("query_relationship", {
            "entity_id": service.id,
            "relationship_type": "USES",
            "direction": "incoming",
        })
        
        relationships = result.get("results", [])
        return [
            {
                "service_id": r["source_id"],
                "relationship": r["type"],
                "purpose": r.get("properties", {}).get("purpose", ""),
            }
            for r in relationships
        ]
    
    async def search_services(self, query: str) -> List[Dict]:
        """Full-text search for services."""
        result = await self._call_mcp_tool("search_graph", {
            "query": query,
            "entity_types": ["Service"],
        })
        
        return result.get("results", [])
    
    async def get_context_for_query(self, query: str) -> str:
        """
        Get Knowledge Graph context to enhance LLM responses.
        
        This extracts relevant entities and relationships from the graph
        to provide the LLM with accurate infrastructure context.
        """
        # Search for relevant entities
        search_results = await self._call_mcp_tool("search_graph", {"query": query})
        
        if not search_results.get("results"):
            return ""
        
        # Build context from top results
        context_parts = []
        for entity in search_results["results"][:3]:  # Top 3
            entity_id = entity["id"]
            
            # Get detailed context
            ctx_result = await self._call_mcp_tool("get_entity_context", {
                "entity_id": entity_id,
            })
            
            if ctx_result.get("entity"):
                context_parts.append(self._format_entity_context(ctx_result))
        
        if context_parts:
            return "\n\n".join([
                "## Knowledge Graph Context:",
                *context_parts,
            ])
        
        return ""
    
    def _format_entity_context(self, context: Dict) -> str:
        """Format entity context for LLM consumption."""
        entity = context.get("entity", {})
        relationships = context.get("direct_relationships", [])
        dependents = context.get("dependents", [])
        
        lines = [f"**{entity.get('name', 'Unknown')}**: {entity.get('type', 'Entity')}"")
        
        if relationships:
            lines.append("  - Relationships:")
            for rel in relationships:
                lines.append(f"    - {rel['type']} → {rel['to']}")
        
        if dependents:
            lines.append("  - Used by:")
            for dep in dependents:
                lines.append(f"    - {dep['from']} ({dep['type']})")
        
        return "\n".join(lines)


# Singleton client
_kg_client: Optional[KnowledgeGraphClient] = None


async def get_knowledge_graph_client() -> KnowledgeGraphClient:
    """Get the global Knowledge Graph client."""
    global _kg_client
    if _kg_client is None:
        _kg_client = KnowledgeGraphClient()
    return _kg_client


async def knowledge_graph_query(query: str) -> str:
    """
    Tool function: Query the Knowledge Graph.
    
    Used by LLM to get infrastructure context for responses.
    """
    client = await get_knowledge_graph_client()
    return await client.get_context_for_query(query)


async def get_service_status_from_graph(service_name: str) -> Dict:
    """
    Tool function: Get comprehensive service status from Knowledge Graph.
    
    Returns dependencies, integrations, and health context.
    """
    client = await get_knowledge_graph_client()
    
    service = await client.find_service(service_name)
    if not service:
        return {"error": f"Service '{service_name}' not found in Knowledge Graph"}
    
    dependencies = await client.get_service_dependencies(service_name)
    integrations = await client.get_service_integrations(service_name)
    
    return {
        "service": {
            "name": service.name,
            "type": service.type,
            "properties": service.properties,
        },
        "dependencies": dependencies,
        "integrations": integrations,
        "status": service.properties.get("status", "unknown"),
    }
