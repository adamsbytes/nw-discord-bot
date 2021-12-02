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
from logging.handlers import RotatingFileHandler

import boto3
import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from botocore.exceptions import ClientError
from discord.ext import tasks
from discord_slash import SlashCommand
from discord_slash.utils.manage_commands import create_choice, create_option
from dotenv import dotenv_values

# Need a better way to determine this
if 'LOGNAME' not in os.environ: # logname is env var on ec2, not on local dev
    DEV_MODE = True
    _FILE_PREFIX = ''
    SCHEDULER_CONFIG_FILEPATH = None
else:
    DEV_MODE = False
    _FILE_PREFIX = '/opt/invasion-bot/'
    SCHEDULER_CONFIG_FILEPATH = f'{_FILE_PREFIX}announcement_schedules.json'

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

CITIES_WITH_ANNOUNCE_ENABLED = []
TODAYS_CITIES_WITH_INVASIONS = []
TOMORROWS_CITIES_WITH_INVASIONS = []

# Load configuration
try:
    config = {
        **dotenv_values(f'{_FILE_PREFIX}.env'),
        **dotenv_values(f'{_FILE_PREFIX}.env.secret'),
        **os.environ # override .env vars with os environment vars
    }
    if SCHEDULER_CONFIG_FILEPATH is not None:
        with open(SCHEDULER_CONFIG_FILEPATH, encoding='utf-8') as f:
            scheduling_config = json.load(f)
        for sched_city in scheduling_config:
            CITIES_WITH_ANNOUNCE_ENABLED.append(sched_city)
            CITY_INFO[sched_city]['announcement_channel_id'] = int(scheduling_config[sched_city]['announcement_channel_id'])
            CITY_INFO[sched_city]['announcement_hour'] = int(scheduling_config[sched_city]['announcement_hour'])
            CITY_INFO[sched_city]['announcement_minute'] = int(scheduling_config[sched_city]['announcement_minute'])
            assert len(str(CITY_INFO[sched_city]['announcement_channel_id'])) == 18
            assert CITY_INFO[sched_city]['announcement_hour'] <= 24
            assert CITY_INFO[sched_city]['announcement_hour'] > 0
            assert CITY_INFO[sched_city]['announcement_minute'] < 60
            assert CITY_INFO[sched_city]['announcement_minute'] >= 0
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

bot = discord.Client(intents=discord.Intents.all())
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
        try:
            logger.debug('Attempting to start scheduler')
            scheduler = AsyncIOScheduler()
            for city in CITIES_WITH_ANNOUNCE_ENABLED:
                logger.debug(f'Adding job to scheduler for announcements in {city}')
                scheduler.add_job(
                    send_city_invasion_announcement,
                    trigger=CronTrigger(
                        hour=str(CITY_INFO[city]['announcement_hour']),
                        minute=str(CITY_INFO[city]['announcement_minute']),
                        second="0"),
                    args=[city]
                )
            logger.debug('Adding job to refresh invasion data daily at midnight')
            scheduler.add_job(
                refresh_invasion_data,
                trigger=CronTrigger(hour="0", minute="0", second="0")
            ) # daily task at midnight
            scheduler.start()
        except Exception as sched_exception:
            logger.exception(f'Failed to start scheduler: {sched_exception}')
        else:
            logger.debug('Initialized scheduler and added channel-announce function')
    info_gather.start()

async def clear_invasion_data_lists() -> None:
    '''This clears TODAYS/TOMORROWS_CITIES_WITH_INVASIONS lists'''
    # Need a better way to do this, doing it within the refresh function
    # causes a scoping issue with the variable
    TODAYS_CITIES_WITH_INVASIONS.clear()
    TOMORROWS_CITIES_WITH_INVASIONS.clear()

async def convert_time_str_to_min_sec(hour) -> int:
    '''Intakes a string with style 08:30 PM and returns 24-hour format time int: 20'''
    in_hour = int(hour.split(':')[0]) # split 08:30 PM style string to 08
    in_minute = int(hour.split(':')[1].split(' ')[0]) # split 8:30 PM style string to 30
    in_ampm = hour.split(' ')[1] # split 08:30 PM style string to PM
    if in_ampm == 'PM':
        in_hour += 12
    return in_hour, in_minute

async def get_all_invasion_string(day: str = None) -> str:
    '''Returns a string detailing what invasions are occuring on [day]'''
    if day == 'today' or day is None:
        # this sorts today's invasions returned by their time
        today_invasion_text = []
        if TODAYS_CITIES_WITH_INVASIONS: # if any invasions today
            todays_cities_and_windows = {}
            for today_city in TODAYS_CITIES_WITH_INVASIONS:
                if await is_hour_in_future(CITY_INFO[today_city]['siege_time']):
                    todays_cities_and_windows[today_city] = CITY_INFO[today_city]['siege_time']
            sorted_partial = sorted(todays_cities_and_windows, key = todays_cities_and_windows.get)
            for key in sorted_partial:
                today_invasion_text.append(f"{key} at {CITY_INFO[key]['siege_time']}")
        # determine today's response
        if len(today_invasion_text) > 2:
            today_invasion_str = ', '.join(today_invasion_text)
            today_response = f'Today there are {str(len(today_invasion_text))} invasions: {today_invasion_str}'
        elif len(today_invasion_text) == 2:
            today_response = f'Today there are 2 invasions: {today_invasion_text[0]} and {today_invasion_text[1]}'
        elif len(today_invasion_text) == 1:
            today_response = f'Today there is one invasion: {today_invasion_text[0]}'
        else:
            today_response = 'There are no invasions happening today!'
    if day == 'tomorrow' or day is None:
        # this sorts tomorrow's invasions returned by their time
        tomorrow_invasion_text = []
        if TOMORROWS_CITIES_WITH_INVASIONS: # if any invasions tomorrow
            tomorrows_cities_and_windows = {}
            for tomorrow_city in TOMORROWS_CITIES_WITH_INVASIONS:
                tomorrows_cities_and_windows[tomorrow_city] = CITY_INFO[tomorrow_city]['siege_time']
            sorted_partial = sorted(tomorrows_cities_and_windows, key = tomorrows_cities_and_windows.get)
            for key in sorted_partial:
                tomorrow_invasion_text.append(f"{key} at {CITY_INFO[key]['siege_time']}")
        # determine tomorrow's response
        if len(tomorrow_invasion_text) > 2:
            tomorrow_invasion_str = ', '.join(tomorrow_invasion_text)
            tomorrow_response = f'Tomorrow there are {str(len(tomorrow_invasion_text))} invasions: {tomorrow_invasion_str}'
        elif len(tomorrow_invasion_text) == 2:
            tomorrow_response = f'Tomorrow there are 2 invasions: {tomorrow_invasion_text[0]} and {tomorrow_invasion_text[1]}'
        elif len(tomorrow_invasion_text) == 1:
            tomorrow_response = f'Tomorrow there is one invasion: {tomorrow_invasion_text[0]}'
        else:
            tomorrow_response = 'There are no invasions happening tomorrow!'
    if day is None:
        response = today_response + '\n' + tomorrow_response
    elif day == 'today':
        response = today_response
    elif day == 'tomorrow':
        response = tomorrow_response

    return response

async def get_city_invasion_string(city, day=None) -> str:
    '''Returns a string detailing invasion status for a [city] on [day] or both today/tomorrow if [day=None](default)'''
    siege_window_in_future = await is_hour_in_future(CITY_INFO[city]['siege_time'])
    if day is None: # both days
        # if invasion later and it is not siege time yet
        if (city in TODAYS_CITIES_WITH_INVASIONS) and siege_window_in_future:
            duration_str = await get_time_til_hour(CITY_INFO[city]['siege_time'])
            response = f"{city} has an invasion later today in {duration_str} at {CITY_INFO[city]['siege_time']} EST"
        # if invasion happened earlier today
        if (city in TODAYS_CITIES_WITH_INVASIONS) and not siege_window_in_future:
            response = f"{city} had an invasion earlier today at {CITY_INFO[city]['siege_time']} EST"
        # if invasion is tomorrow
        if city in TOMORROWS_CITIES_WITH_INVASIONS:
            response = f"{city} has an invasion tomorrow at {CITY_INFO[city]['siege_time']} EST"
        # if no invasions next two days
        if (city not in TODAYS_CITIES_WITH_INVASIONS) and (city not in TOMORROWS_CITIES_WITH_INVASIONS):
            response = f"{city} does not have any invasions today or tomorrow!"
    elif day == 'tomorrow': # tomorrow
        if city in TOMORROWS_CITIES_WITH_INVASIONS:
            response = f"{city} has an invasion tomorrow at {CITY_INFO[city]['siege_time']} EST"
        else:
            response = f"{city} does not have an invasion tomorrow!"
    else: # assume today otherwise
        # if invasion later and it is not siege time yet
        if (city in TODAYS_CITIES_WITH_INVASIONS) and siege_window_in_future:
            duration_str = await get_time_til_hour(CITY_INFO[city]['siege_time'])
            response = f"{city} has an invasion later today in {duration_str} at {CITY_INFO[city]['siege_time']} EST"
        # if invasion happened earlier today
        if (city in TODAYS_CITIES_WITH_INVASIONS) and not siege_window_in_future:
            response = f"{city} had an invasion earlier today at {CITY_INFO[city]['siege_time']} EST"
        # if no invasion today
        if city not in TODAYS_CITIES_WITH_INVASIONS:
            response = f"{city} does not have an invasion today!"
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

async def refresh_invasion_data() -> None:
    '''Clears locally cached invasion lists and gets invasion status from dynamodb for all cities'''
    logger.debug('Attempting to refresh_invasion_data()')
    await clear_invasion_data_lists()

    for c_name in list(CITY_INFO.keys()):
        logger.debug(f'Refreshing data in {c_name}')
        city_name = ''.join(e for e in c_name if e.isalnum()).lower()
        city_db_table = f"{config['EVENT_TABLE_PREFIX']}{city_name}"
        # Get today's invasions
        logger.debug(f"Attempting to find today's invasions in table: {city_db_table}")
        today_search_date = str(datetime.date.today().strftime('%Y-%m-%d'))
        response = db.get_item(
            TableName=city_db_table,
            Key = {
                'date': {'S': str(today_search_date)}
            }
        )
        logger.debug(f'Response from db: {response}')
        if 'Item' in response:
            logger.debug(f"Determined invasion happening today in {c_name}")
            TODAYS_CITIES_WITH_INVASIONS.append(c_name)
        else:
            logger.debug(f"Determined no invasion is happening today in {c_name}")
        # Get tomorrow's invasions
        logger.debug(f"Attempting to find tomorrow's invasions in table: {city_db_table}")
        tomorrow_search_date = str((datetime.date.today() + datetime.timedelta(days=1)).strftime('%Y-%m-%d'))
        response = db.get_item(
            TableName=city_db_table,
            Key = {
                'date': {'S': str(tomorrow_search_date)}
            }
        )
        logger.debug(f'Response from db: {response}')
        if 'Item' in response:
            logger.debug(f"Determined invasion happening tomorrow in {c_name}")
            TOMORROWS_CITIES_WITH_INVASIONS.append(c_name)
        else:
            logger.debug(f"Determined no invasion is happening tomorrow in {c_name}")

    logger.debug('Completed running refresh_invasion_data()')

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

async def send_city_invasion_announcement(city):
    '''Sends a city invasion announcement to [city] if the city has that enabled. See announcement_schedules.json'''
    logger.debug(f'Attempting to send_city_invasion_announcement() for city: {city}')
    if (city in CITIES_WITH_ANNOUNCE_ENABLED) and (city in TODAYS_CITIES_WITH_INVASIONS):
        allowed_mentions = discord.AllowedMentions(everyone=True)
        announcement_message = \
            f"@everyone there is an invasion today in {city} at {CITY_INFO[city]['siege_time']}. " + \
            'Please do not forget to sign up at the War Board in town. Remember to sign up early ' + \
            'to help ensure you get a spot!'
        announcement_channel_id = bot.get_channel(CITY_INFO[city]['announcement_channel_id'])
        logger.debug(f"Sending announcement message for {city} to {str(CITY_INFO[city]['announcement_channel_id'])}")
        await announcement_channel_id.send(announcement_message, allowed_mentions=allowed_mentions)
    elif city not in CITIES_WITH_ANNOUNCE_ENABLED:
        logger.debug(f'Could not find {city} in enabled city list: {CITIES_WITH_ANNOUNCE_ENABLED}')
    else:
        logger.debug(f'Determined {city} does not have an invasion today, no announcement needed.')

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

@slash.slash(name='invasions',
            description='Responds with all invasions happening in the next two days',
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
async def invasions(ctx, city: str = None, day: str = None):
    '''Responds to /invasions command with all invasions happening for the city, or for today sorted by time'''
    logger.info(f'/invasions [city: {city}] [day: {day}] invoked')

    if city is None:
        response = await get_all_invasion_string(day)
    else:
        response = await get_city_invasion_string(city, day)

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

@tasks.loop(hours = 1)
async def info_gather():
    '''Executes referesh_invasion_data and refresh_siege_window every hour or on command'''
    logger.info('Attempting to run scheduled task info_gather()')
    await refresh_invasion_data()
    await refresh_siege_window()
    logger.info('Completed running scheduled task info_gather()')

bot.run(config['DISCORD_TOKEN'])
