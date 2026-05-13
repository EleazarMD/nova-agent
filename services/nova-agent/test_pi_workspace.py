import asyncio
from nova.pi_workspace import list_pages, create_page, create_block

async def main():
    pages = await list_pages()
    print("Pages:", len(pages))
    page = await create_page("Test Page via Script", icon="🤖")
    print("Created Page:", page)
    if page:
        block = await create_block(page["id"], "paragraph", {"richText": [{"type": "text", "text": {"content": "This is a test block"}, "plainText": "This is a test block"}]})
        print("Created Block:", block)

if __name__ == "__main__":
    asyncio.run(main())
