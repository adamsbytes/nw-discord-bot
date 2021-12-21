# discord_bot.py
''' This module runs the invasion-bot application'''
# Disable:
#   C0206: dict-items (poor suggestion, may revist)
#   C0301: line length (unavoidable)
#   R0912: too many branches (TODO)
#   R0915: too many statements (TODO)
#   W0703: exception is too general (TODO)
#   W1203: logging with f-string (this works fine, plan to continue using)
# pylint: disable=C0206,C0301,R0912,R0915,W0703,W1203

import datetime
import json
import logging
import math
import os
import sys
import time
from logging.handlers import RotatingFileHandler

import boto3
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from botocore.exceptions import ClientError
from discord_slash import SlashCommand
from discord_slash.utils.manage_commands import create_choice, create_option
from dotenv import dotenv_values
from utils.discord_commands import DiscordCommands
from world_status import NWWorldStatusClient

# Need a better way to determine this
if 'LOGNAME' not in os.environ: # logname is env var on ec2, not on local dev
    DEV_MODE = True
    _FILE_PREFIX = ''
    EVENTS_CONFIG_FILEPATH = None
    GUILD_EVENTS_CONFIG_FILEPATH = None
    WORLD_UPDATES_CONFIG_FILEPATH = None
else:
    DEV_MODE = False
    _FILE_PREFIX = '/opt/invasion-bot/'
    EVENTS_CONFIG_FILEPATH = f'{_FILE_PREFIX}channel_events.json'
    GUILD_EVENTS_CONFIG_FILEPATH = f'{_FILE_PREFIX}guild_events.json'
    WORLD_UPDATES_CONFIG_FILEPATH = f'{_FILE_PREFIX}world_updates.json'

CITY_INFO = {
    'Brightwood': {},
    'Cutlass Keys': {},
    'Ebonscale Reach': {},
    'Everfall': {},
    'First Light': {},
    "Monarch's Bluffs": {},
    'Mourningdale': {},
    'Reekwater': {},
    'Restless Shore': {},
    'Windsward': {},
    "Weaver's Fen": {}
}

CHANNELS_WITH_ANNOUNCE_ENABLED = {}
GUILDS_WITH_EVENT_CREATION_ENABLED = []
WORLDS_WITH_STATUS_UPDATE_ENABLED = {}
TODAYS_CITIES_WITH_EVENTS = []
TOMORROWS_CITIES_WITH_EVENTS = []
UPCOMING_EVENT_INFO = {}

# Load configuration
try:
    config = {
        **dotenv_values(f'{_FILE_PREFIX}.env'),
        **dotenv_values(f'{_FILE_PREFIX}.env.secret'),
        **os.environ # override .env vars with os environment vars
    }
    if EVENTS_CONFIG_FILEPATH is not None:
        with open(EVENTS_CONFIG_FILEPATH, encoding='utf-8') as f:
            events_config = json.load(f)
        for channel_id in events_config:
            assert len(channel_id) == 18
            assert events_config[channel_id]['event_hour'] <= 24
            assert events_config[channel_id]['event_hour'] > 0
            assert events_config[channel_id]['event_minute'] < 60
            assert events_config[channel_id]['event_minute'] >= 0
            if events_config[channel_id]['event_type'] == 'announcement':
                CHANNELS_WITH_ANNOUNCE_ENABLED[channel_id] = {}
                CHANNELS_WITH_ANNOUNCE_ENABLED[channel_id]['city'] = events_config[channel_id]['announcement_city']
                CHANNELS_WITH_ANNOUNCE_ENABLED[channel_id]['hour'] = int(events_config[channel_id]['event_hour'])
                CHANNELS_WITH_ANNOUNCE_ENABLED[channel_id]['minute'] = int(events_config[channel_id]['event_minute'])
    if GUILD_EVENTS_CONFIG_FILEPATH is not None:
        with open(GUILD_EVENTS_CONFIG_FILEPATH, encoding='utf-8') as f:
            guild_events_config = json.load(f)
        GUILDS_WITH_EVENT_CREATION_ENABLED = guild_events_config['guilds_with_event_creation_enabled']
    if WORLD_UPDATES_CONFIG_FILEPATH is not None:
        with open(WORLD_UPDATES_CONFIG_FILEPATH, encoding='utf-8') as f:
            world_updates_config = json.load(f)
        WORLDS_WITH_STATUS_UPDATE_ENABLED = world_updates_config
except Exception as e:
    sys.exit(f'Failed to load configuration: {e}')

# Configure logging
try:
    logger = logging.getLogger(config['LOGGER_NAME'])
    logger.setLevel(logging.DEBUG)
    if DEV_MODE: # no need for rotating logs while developing
        file_handler = logging.FileHandler(f"{_FILE_PREFIX}{config['LOG_FILE_NAME']}")
    else:
        file_handler = RotatingFileHandler(
            f"{_FILE_PREFIX}{config['LOG_FILE_NAME']}",
            maxBytes=2097152,
            backupCount=3
        ) # keeps up to 4 2MB logs
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter('%(asctime)s - %(name)-16s - %(levelname)-8s - %(message)s')
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)
except Exception as e:
    sys.exit(f"Could not initalize logger with name {config['LOGGER_NAME']}: {e}")
else:
    logger.debug('Logger initialized')

event_client = DiscordCommands(token=config['DISCORD_TOKEN'])
bot = discord.Client(
    intents=discord.Intents.all(),
    activity=discord.Game(name='New World')
)
slash = SlashCommand(bot, sync_commands=True)
try:
    if DEV_MODE:
        logger.debug('Attempting to initialize dev Boto3 dynamodb session')
        db = boto3.Session(profile_name=config['DEV_AWS_PROFILE']).client('dynamodb')
    else:
        logger.debug('Attempting to initialize prod Boto3 dynamodb session')
        db = boto3.client('dynamodb', region_name=config['AWS_REGION'])
except ClientError as e:
    logger.exception('Failed to initalize boto3 session')
else:
    logger.debug('Initialized Boto3 dynamodb session')

@bot.event
async def on_ready():
    '''This function is activated when the bot reaches a 'ready' state.'''
    logger.info('Bot is ready')
    if not DEV_MODE:
        try: # scheduler startup
            logger.debug('Attempting to start scheduler')
            scheduler = AsyncIOScheduler()
            for channel in CHANNELS_WITH_ANNOUNCE_ENABLED:
                logger.debug(f'Adding job to scheduler for announcements in {str(channel)}')
                job_hour = CHANNELS_WITH_ANNOUNCE_ENABLED[channel]['hour']
                job_minute = CHANNELS_WITH_ANNOUNCE_ENABLED[channel]['minute']
                job_city = CHANNELS_WITH_ANNOUNCE_ENABLED[channel]['city']
                scheduler.add_job(
                    send_city_event_announcement,
                    trigger=CronTrigger(
                        hour=str(job_hour),
                        minute=str(job_minute),
                        second="0"
                        ),
                    args=[int(channel), job_city]
                )
            for world in WORLDS_WITH_STATUS_UPDATE_ENABLED:
                logger.debug(f'Adding job to scheduler for world status updates for {world}')
                job_channel_list = WORLDS_WITH_STATUS_UPDATE_ENABLED[world]
                job_update_client = NWWorldStatusClient('us-east', world)
                scheduler.add_job(
                    send_world_status_if_changed,
                    'interval',
                    minutes=1,
                    args=[job_channel_list, job_update_client, world]
                )
            logger.debug('Adding job to refresh invasion data daily at midnight')
            scheduler.add_job(
                refresh_event_data,
                trigger=CronTrigger(hour="0", minute="0", second="1")
            ) # daily task at midnight
            logger.debug('Adding job to refresh siege windows daily 15 minutes after midnight')
            scheduler.add_job(
                refresh_siege_window,
                trigger=CronTrigger(hour="0", minute="15", second="0")
            ) # daily task at 00:15
            logger.debug('Adding job to refresh invasion data daily 15 minutes after midnight')
            scheduler.add_job(
                refresh_event_data,
                trigger=CronTrigger(hour="0", minute="15", second="0")
            ) # daily task at 00:15
            logger.debug('Adding job to update guild events daily 20 minutes after midnight')
            scheduler.add_job(
                update_guild_events,
                trigger=CronTrigger(hour="0", minute="20", second="0")
            ) # daily task at 00:15
            scheduler.start()
        except Exception as sched_exception:
            logger.exception(f'Failed to start scheduler: {sched_exception}')
        else:
            logger.debug('Initialized scheduler successfully')
            await refresh_siege_window()
            await refresh_event_data()
            await update_guild_events()
            logger.debug('Completed on ready')

async def clear_event_data_lists() -> None:
    '''This clears UPCOMING_EVENT_INFO list'''
    # Need a better way to do this, doing it within the refresh function
    # causes a scoping issue with the variable
    TODAYS_CITIES_WITH_EVENTS.clear()
    TOMORROWS_CITIES_WITH_EVENTS.clear()
    UPCOMING_EVENT_INFO.clear()

async def convert_time_str_to_min_sec(hour) -> int:
    '''Intakes a string with style 08:30 PM and returns 24-hour format time int: 20'''
    in_hour = int(hour.split(':')[0]) # split 08:30 PM style string to 08
    in_minute = int(hour.split(':')[1].split(' ')[0]) # split 8:30 PM style string to 30
    in_ampm = hour.split(' ')[1] # split 08:30 PM style string to PM
    if in_ampm == 'PM':
        in_hour += 12
    return in_hour, in_minute

async def get_all_event_string(day: str = None) -> str:
    '''Returns a string detailing the server's events on [day] or today/tomorrow if [day=None]'''
    if day == 'today' or day is None:
        today_event_text = []
        if TODAYS_CITIES_WITH_EVENTS: # if any events today
            todays_cities_and_windows = {}
            for today_city in TODAYS_CITIES_WITH_EVENTS:
                if await is_hour_in_future(CITY_INFO[today_city]['siege_time']):
                    todays_cities_and_windows[today_city] = CITY_INFO[today_city]['siege_time']
            sorted_partial = sorted(todays_cities_and_windows, key = todays_cities_and_windows.get)
            for key in sorted_partial:
                today_event_text.append(f"    {CITY_INFO[key]['siege_time']} EST - {str(UPCOMING_EVENT_INFO[key]['event_type']).capitalize()} in {key}")
        # determine today's response
        if len(today_event_text) > 1:
            today_invasion_str = '\n'.join(today_event_text)
            today_response = f'Today there are {str(len(today_event_text))} events:\n{today_invasion_str}'
        elif len(today_event_text) == 1:
            today_response = f'Today there is 1 event:\n{today_event_text[0]}'
        else:
            today_response = 'There are no events happening today!'
    if day == 'tomorrow' or day is None:
        tomorrow_event_text = []
        if TOMORROWS_CITIES_WITH_EVENTS: # if any events today
            tomorrows_cities_and_windows = {}
            for tomorrow_city in TOMORROWS_CITIES_WITH_EVENTS:
                if await is_hour_in_future(CITY_INFO[tomorrow_city]['siege_time']):
                    tomorrows_cities_and_windows[tomorrow_city] = CITY_INFO[tomorrow_city]['siege_time']
            sorted_partial = sorted(tomorrows_cities_and_windows, key = tomorrows_cities_and_windows.get)
            for key in sorted_partial:
                tomorrow_event_text.append(f"    {CITY_INFO[key]['siege_time']} EST - {str(UPCOMING_EVENT_INFO[key]['event_type']).capitalize()} in {key}")
        # determine tomorrow's response
        if len(tomorrow_event_text) > 1:
            tomorrow_invasion_str = '\n'.join(tomorrow_event_text)
            tomorrow_response = f'Tomorrow there are {str(len(tomorrow_event_text))} events:\n{tomorrow_invasion_str}'
        elif len(tomorrow_event_text) == 1:
            tomorrow_response = f'Tomorrow there is 1 event:\n{tomorrow_event_text[0]}'
        else:
            tomorrow_response = 'There are no events happening tomorrow!'
    if day is None:
        response = today_response + '\n' + tomorrow_response
    elif day == 'today':
        response = today_response
    elif day == 'tomorrow':
        response = tomorrow_response

    return response

async def get_city_event_string(city, day=None) -> str:
    '''Returns a string detailing event status for a [city] on [day] or both today/tomorrow if [day=None](default)'''
    siege_window_in_future = await is_hour_in_future(CITY_INFO[city]['siege_time'])
    if city in UPCOMING_EVENT_INFO and 'event_type' in UPCOMING_EVENT_INFO[city]:
        if str(UPCOMING_EVENT_INFO[city]['event_type']).capitalize() == 'Invasion':
            event_str = 'an invasion'
        else:
            event_str = 'a war'
    if day is None: # both days
        # if event later and it is not siege time yet
        if (city in TODAYS_CITIES_WITH_EVENTS) and siege_window_in_future:
            duration_str = await get_time_til_hour(CITY_INFO[city]['siege_time'])
            response = f"{city} has {event_str} later today in {duration_str} at {CITY_INFO[city]['siege_time']} EST"
        # if event happened earlier today
        if (city in TODAYS_CITIES_WITH_EVENTS) and not siege_window_in_future:
            response = f"{city} had {event_str} earlier today at {CITY_INFO[city]['siege_time']} EST"
        # if event is tomorrow
        if city in TOMORROWS_CITIES_WITH_EVENTS:
            response = f"{city} has {event_str} tomorrow at {CITY_INFO[city]['siege_time']} EST"
        # if no events next two days
        if (city not in TODAYS_CITIES_WITH_EVENTS) and (city not in TOMORROWS_CITIES_WITH_EVENTS):
            response = f"{city} does not have any events today or tomorrow!"
    elif day == 'tomorrow': # tomorrow
        if city in TOMORROWS_CITIES_WITH_EVENTS:
            response = f"{city} has {event_str} tomorrow at {CITY_INFO[city]['siege_time']} EST"
        else:
            response = f"{city} does not have any events tomorrow!"
    else: # assume today otherwise
        # if event later and it is not siege time yet
        if (city in TODAYS_CITIES_WITH_EVENTS) and siege_window_in_future:
            duration_str = await get_time_til_hour(CITY_INFO[city]['siege_time'])
            response = f"{city} has {event_str} later today in {duration_str} at {CITY_INFO[city]['siege_time']} EST"
        # if event happened earlier today
        if (city in TODAYS_CITIES_WITH_EVENTS) and not siege_window_in_future:
            response = f"{city} had {event_str} earlier today at {CITY_INFO[city]['siege_time']} EST"
        # if no event today
        if city not in TODAYS_CITIES_WITH_EVENTS:
            response = f"{city} does not have any events today!"
    return response

async def get_time_til_hour(hour) -> str:
    '''Returns a string with style 1h1m with the duration from now until [hour] with style 08:30 PM'''
    logger.debug(f'Attempting to get_time_til_hour() for: {hour}')
    hour_int, minute_int = await convert_time_str_to_min_sec(hour)
    hour_24fmt = datetime.date.today().strftime('%Y-%m-%d') + \
         f' {str(hour_int)}:{str(minute_int)}:00'
    now_24fmt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    hour_obj = datetime.datetime.strptime(hour_24fmt, '%Y-%m-%d %H:%M:%S')
    now_obj = datetime.datetime.strptime(now_24fmt, '%Y-%m-%d %H:%M:%S')
    time_delta_seconds = (hour_obj - now_obj).total_seconds()
    duration_hours = math.floor(time_delta_seconds / 3600)
    remaining_seconds = time_delta_seconds - (duration_hours * 3600)
    duration_minutes = math.ceil(remaining_seconds / 60)
    if duration_minutes == 60:
        duration_hours += 1
        duration_minutes = 0
    logger.debug(f'Completed get_time_til_hour() with result: {duration_hours}h{duration_minutes}m')
    return f'{duration_hours}h{duration_minutes}m'

async def is_hour_in_future(hour) -> bool:
    '''Returns a bool that is True if [hour] is after now. Standard format: 8:00 PM'''
    logger.debug(f'Attempting to is_hour_in_future() for: {hour}')
    hour_int, minute_int = await convert_time_str_to_min_sec(hour)
    logger.debug(f'Converted hour to {hour_int}, minute to {minute_int}')
    if hour_int <1 or hour_int > 24 or not isinstance(hour_int, int):
        logger.exception(f'Cannot operate on hour: {hour_int}')
    time_now = datetime.datetime.now()
    time_hour = time_now.replace(hour=hour_int, minute=minute_int)
    result = time_now < time_hour
    logger.debug(f'Completed is_hour_in_future() with hour {hour_int}:{minute_int} and got result: {result}')
    return result

async def refresh_event_data() -> None:
    '''Clears locally cached event lists and gets event status from dynamodb for all cities'''
    logger.debug('Attempting to refresh_event_data()')
    await clear_event_data_lists()

    for c_name in list(CITY_INFO.keys()):
        logger.debug(f'Refreshing data in {c_name}')
        city_name = ''.join(e for e in c_name if e.isalnum()).lower()
        city_db_table = f"{config['EVENT_TABLE_PREFIX']}{city_name}"
        # Get today's events
        logger.debug(f"Attempting to find today's events in table: {city_db_table}")
        today_search_date = str(datetime.date.today().strftime('%Y-%m-%d'))
        response = db.get_item(
            TableName=city_db_table,
            Key = {
                'date': {'S': str(today_search_date)}
            }
        )
        logger.debug(f'Response from db: {response}')
        if 'Item' in response:
            logger.debug(f"Determined event happening today in {c_name}")
            UPCOMING_EVENT_INFO[c_name] = {
                'event_type': response['Item']['type']['S'],
                'event_date': str(today_search_date),
                'event_attacker': response['Item']['attacker']['S'],
                'event_defender': response['Item']['defender']['S']
            }
            TODAYS_CITIES_WITH_EVENTS.append(c_name)
        else:
            logger.debug(f"Determined no event is happening today in {c_name}")
        # Get tomorrow's invasions
        logger.debug(f"Attempting to find tomorrow's events in table: {city_db_table}")
        tomorrow_search_date = str((datetime.date.today() + datetime.timedelta(days=1)).strftime('%Y-%m-%d'))
        response = db.get_item(
            TableName=city_db_table,
            Key = {
                'date': {'S': str(tomorrow_search_date)}
            }
        )
        logger.debug(f'Response from db: {response}')
        if 'Item' in response:
            logger.debug(f"Determined event happening today in {c_name}")
            UPCOMING_EVENT_INFO[c_name] = {
                'event_type': response['Item']['type']['S'],
                'event_date': str(tomorrow_search_date),
                'event_attacker': response['Item']['attacker']['S'],
                'event_defender': response['Item']['defender']['S']
            }
            TOMORROWS_CITIES_WITH_EVENTS.append(c_name)
        else:
            logger.debug(f"Determined no event is happening tomorrow in {c_name}")

    logger.debug('Completed running refresh_event_data()')

async def refresh_siege_window(city:str = None) -> None:
    '''Gets siege window data from dynamodb for [city] or all cities if [city=None] (default)'''
    logger.debug(f'Attempting to refresh_siege_window({city})')
    table_name = config['SIEGE_INFO_TABLE_NAME']

    if city:
        cities_to_refresh = [city]
    else:
        cities_to_refresh = list(CITY_INFO.keys())

    for city_name in cities_to_refresh:
        logger.debug(f'Refreshing data in {city_name}')
        response = db.get_item(
            TableName=table_name,
            Key = {
                'city': {'S': city_name}
            }
        )
        CITY_INFO[city_name]['siege_time'] = response['Item']['time']['S']
        logger.debug(f"Determined siege time in {city_name}: {CITY_INFO[city_name]['siege_time']}")
    logger.debug('Completed running refresh_siege_window()')

async def send_city_event_announcement(int_channel_id: int, city: str):
    '''Sends a city event announcement to [channel] for [city]. See channel_events.json'''
    logger.debug(f'Attempting to send_city_invasion_announcement() to channel: {str(int_channel_id)} for city: {city}')
    if city in UPCOMING_EVENT_INFO:
        if UPCOMING_EVENT_INFO[city]['event_date'] == str(datetime.date.today().strftime('%Y-%m-%d')):
            announcement_channel = bot.get_channel(int_channel_id)
            allowed_mentions = discord.AllowedMentions(everyone=True)
            announcement_message = \
                f"@everyone don't forget to sign up for the {UPCOMING_EVENT_INFO[city]['event_type']} today in {city} at {CITY_INFO[city]['siege_time']}. " + \
                'Remember to sign up early to help ensure you get a spot!'
            logger.debug(f"Sending announcement message for {city} to {str(int_channel_id)}")
            await announcement_channel.send(announcement_message, allowed_mentions=allowed_mentions)
    else:
        logger.debug(f'Determined {city} does not have an invasion today, no announcement needed.')

async def send_world_status_if_changed(channel_id_list: list, status_client: NWWorldStatusClient, world_name: str):
    '''Sends a message if the world status changes to [channel]. See channel_events.json'''
    logger.debug(f'Checking if {world_name} status has changed')
    old_world_status = status_client.world_status
    if status_client.has_world_status_changed():
        logger.debug('Determined world status has changed')
        new_world_status = status_client.world_status
        update_message = f"{world_name}'s status has changed from {old_world_status} to {new_world_status}"
        for update_channel_id in channel_id_list:
            logger.debug(f'Sending world status update to {str(update_channel_id)}')
            update_channel = bot.get_channel(int(update_channel_id))
            await update_channel.send(update_message)
    else:
        logger.debug('Determined world status has not changed')

async def update_guild_events():
    '''Creates events that do not already exist in enabled channels'''
    invasion_event_description = 'Available to players level 50+. Sign up at the town board!'

    for guild_id in GUILDS_WITH_EVENT_CREATION_ENABLED:
        logger.debug(f'Adding events for enabled guild with ID: {guild_id}')
        current_guild_event_names = []
        current_guild_events = await event_client.list_guild_events(str(guild_id))
        for event in current_guild_events:
            current_guild_event_names.append(event['name'])
        for city in TODAYS_CITIES_WITH_EVENTS:
            event_type = str(UPCOMING_EVENT_INFO[city]['event_type']).capitalize()
            event_name = f'{event_type} at {city}'
            if event_type == 'War':
                event_description = f"{str(UPCOMING_EVENT_INFO[city]['event_attacker'])} is attacking {str(UPCOMING_EVENT_INFO[city]['event_defender'])}"
            else:
                event_description = invasion_event_description
            logger.debug(f'Found event: [{event_name}] with description: [{event_description}]')
            if event_name not in current_guild_event_names:
                start_time = f"{str(UPCOMING_EVENT_INFO[city]['event_date'])} {CITY_INFO[city]['siege_time']}"
                await event_client.create_guild_event(
                    str(guild_id),
                    event_name,
                    event_description,
                    event_start_est=start_time
                )
                time.sleep(2.5) # attempting to prevent rate limiting
        for city in TOMORROWS_CITIES_WITH_EVENTS:
            event_type = str(UPCOMING_EVENT_INFO[city]['event_type']).capitalize()
            event_name = f'{event_type} at {city}'
            if event_type == 'War':
                event_description = f"{str(UPCOMING_EVENT_INFO[city]['event_attacker'])} is attacking {str(UPCOMING_EVENT_INFO[city]['event_defender'])}"
            else:
                event_description = invasion_event_description
            logger.debug(f'Found event: [{event_name}] with description: [{event_description}]')
            if event_name not in current_guild_event_names:
                start_time = f"{str(UPCOMING_EVENT_INFO[city]['event_date'])} {CITY_INFO[city]['siege_time']}"
                await event_client.create_guild_event(
                    str(guild_id),
                    event_name,
                    event_description,
                    event_start_est=start_time
                )
                time.sleep(2.5) # attempting to prevent rate limiting

city_slash_choice_list = []
for city_choice_name in CITY_INFO:
    city_slash_choice_list.append(
        create_choice(
            name=city_choice_name,
            value=city_choice_name
        )
    )
day_slash_choice_list = [
    create_choice(
        name='Today',
        value='today'
    ),
    create_choice(
        name='Tomorrow',
        value='tomorrow'
    )
]
@slash.slash(name='events',
            description='Responds with all events (wars and invasions) happening in the next two days',
            options=[
                create_option(
                    name='city',
                    description='The city you would like information for',
                    option_type=3,
                    required=False,
                    choices=city_slash_choice_list
                ),
                create_option(
                    name='day',
                    description='The day you would like information for, default is today and tomorrow',
                    option_type=3,
                    required=False,
                    choices=day_slash_choice_list
                )
            ])
async def events(ctx, city: str = None, day: str = None):
    '''Responds to /events command with all events happening for the city, or for today sorted by time'''
    logger.info(f'/events [city: {city}] [day: {day}] invoked')

    if city is None:
        response = await get_all_event_string(day)
    else:
        response = await get_city_event_string(city, day)

    await ctx.send(response)

@slash.slash(name='windows',
            description='Responds with all siege windows in the server'
    )
async def windows(ctx):
    '''Respods to /windows command with a list of siege windows sorted alphabetically'''
    logger.info('/windows invoked')
    window_texts = ['The server siege windows are:']
    cities = []
    for key in CITY_INFO:
        cities.append(key)
    cities.sort()
    for city in cities:
        logger.debug(f"Determined siege time for {city} as {CITY_INFO[city]['siege_time']}")
        window_texts.append(f"{city: <32} {CITY_INFO[city]['siege_time']} EST")
    response = '\n'.join(t for t in window_texts)
    await ctx.send(response)

bot.run(config['DISCORD_TOKEN'])
