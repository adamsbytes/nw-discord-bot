# discord_commands.py
'''Discord commands that are not offered in the standard Discord bot library. WIP.'''
# Disable:
#   C0301: line length (unavoidable)
# pylint: disable=C0301

import datetime
import json
from dateutil import tz

import aiohttp

class DiscordCommands:
    '''Class that handles API communication for events tasks'''
    def __init__(self, token: str) -> None:
        self.__base_url = 'https://discord.com/api/v8'
        self.__auth_headers = {
            'Authorization': f'Bot {token}',
            'User-Agent':'DiscordBot (https://github.com/adamsbytes/nw-discord-bot) Python/3.7 aiohttp/3.8.1',
            'Content-Type':'application/json'
        }

    async def list_guild_events(self, guild_id: str) -> None:
        '''Returns a list of guild events'''
        event_retrieve_url = f'{self.__base_url}/guilds/{guild_id}/scheduled-events'
        async with aiohttp.ClientSession(headers=self.__auth_headers) as session:
            async with session.get(event_retrieve_url) as response:
                return json.loads(await response.read())

    async def create_guild_event(
        self,
        guild_id: str,
        event_name: str,
        event_description: str,
        event_start_est: str
    ) -> None:
        '''Creates a guild event using supplied arguments'''
        event_create_url = f'{self.__base_url}/guilds/{guild_id}/scheduled-events'
        event_start_obj = datetime.datetime.strptime(f'{event_start_est}', '%Y-%m-%d %I:%M %p')
        event_start_time = datetime.datetime.strftime(
            event_start_obj.astimezone(tz.UTC),
            '%Y-%m-%dT%H:%M:%S'
        )
        event_end_time = datetime.datetime.strftime(
            event_start_obj.astimezone(tz.UTC) + datetime.timedelta(minutes=30),
            '%Y-%m-%dT%H:%M:%S'
        )
        event_data = json.dumps({
            'name': event_name,
            'privacy_level': 2,
            'scheduled_start_time': event_start_time,
            'scheduled_end_time': event_end_time,
            'description': event_description,
            'channel_id': None,
            'entity_metadata': {'location': 'Aeternum'},
            'entity_type': 3
        })
        async with aiohttp.ClientSession(headers=self.__auth_headers) as session:
            async with session.post(event_create_url, data=event_data) as response:
                out = await response.read()
                print(out)
            await session.close()
