import asyncio
from nova.tools import handle_manage_workspace

async def main():
    props = {
        "blocks": [
            {
                "type": "image",
                "properties": {"url": "https://images.unsplash.com/photo-1451187580459-43490279c0fa?q=80&w=2072&auto=format&fit=crop"}
            },
            {
                "type": "heading_1",
                "content": "Advanced Canvas Capabilities Test"
            },
            {
                "type": "ai_insight",
                "content": "Nova can now natively build complex UI structures directly inside Pi Workspace using PiCode-parity blocks."
            },
            {
                "type": "mermaid",
                "content": "graph TD;\n    A[Nova Agent] -->|manage_workspace| B(Pi Workspace API);\n    B --> C{Advanced Blocks};\n    C -->|mermaid| D[Diagrams];\n    C -->|ai_insight| E[Insights];\n    C -->|image| F[Media];"
            },
            {
                "type": "callout",
                "content": "This page was generated autonomously by Cascade to test Nova's expanded workspace skill contract.",
                "properties": {"icon": {"type": "emoji", "emoji": "🚀"}, "calloutColor": "blue"}
            }
        ]
    }
    
    print("Sending create_page_with_blocks request...")
    result = await handle_manage_workspace(
        action="create_page_with_blocks",
        title="Nova Advanced Blocks Test",
        icon="🌌",
        properties=props
    )
    print("Result:")
    print(result)

if __name__ == "__main__":
    asyncio.run(main())
