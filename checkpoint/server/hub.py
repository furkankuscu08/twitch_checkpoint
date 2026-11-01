"""Independent bounded writers: a slow browser never blocks another client."""

import asyncio
from dataclasses import dataclass, field

from starlette.websockets import WebSocketDisconnect


@dataclass(eq=False)
class Client:
    socket: object
    audience: str
    queue: asyncio.Queue
    task: asyncio.Task | None = field(default=None)


class Hub:
    def __init__(self, config):
        self.config = config
        self.clients = set()
        self.tasks = set()

    def add(self, socket, audience, snapshot):
        client = Client(
            socket, audience, asyncio.Queue(self.config.WS_CLIENT_QUEUE_SIZE)
        )
        client.queue.put_nowait(snapshot)
        self.clients.add(client)
        client.task = asyncio.create_task(self._write(client))
        self.tasks.add(client.task)
        client.task.add_done_callback(self.tasks.discard)
        return client

    def remove(self, client):
        self.clients.discard(client)
        if client.task:
            client.task.cancel()
        # Cancellation before the writer starts does not enter its finally block.
        task = asyncio.create_task(self._close_socket(client))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def broadcast(self, event, audience):
        for client in tuple(self.clients):
            if client.audience != audience:
                continue
            try:
                client.queue.put_nowait(event)
            except asyncio.QueueFull:
                self.remove(client)

    async def _write(self, client):
        try:
            while True:
                event = await client.queue.get()
                async with asyncio.timeout(self.config.WS_SEND_TIMEOUT_S):
                    await client.socket.send_json(event)
        except (OSError, TimeoutError, RuntimeError, WebSocketDisconnect):
            pass
        finally:
            self.clients.discard(client)
            await self._close_socket(client)

    async def _close_socket(self, client):
        try:
            async with asyncio.timeout(self.config.WS_SEND_TIMEOUT_S):
                await client.socket.close(code=1013)
        except (OSError, TimeoutError, RuntimeError, WebSocketDisconnect):
            pass

    async def close(self):
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.clients.clear()
