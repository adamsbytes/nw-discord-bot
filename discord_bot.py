# discord_bot.py
''' This module runs the invasion-bot application'''
# Disable:
#   dict-items poor suggestion
#   line length (unavoidable)
#   too many branches (TODO)
#   general exception (TODO)
#   logging with f-string
# pylint: disable=C0206,C0301,R0912,W0703,W1203

import datetime
import logging
import os
import sys

import boto3
from botocore.exceptions import ClientError
from discord.ext import commands, tasks
from dotenv import dotenv_values

# Branch log:
# Added function to determine invasion string separately of receiving the command
# Changed functions to async/await where possible
# Changed data injestion to refrence invasions for tomorrow also
# Changed !city command to respond correctly to different days

# TODO: let !invasions tonight/tomorrow/none(all)

# Need a better way to determine this
if 'LOGNAME' not in os.environ: # logname is env var on ec2, not on local dev
    DEV_MODE = True
    _FILE_PREFIX = ''
else:
    DEV_MODE = False
    _FILE_PREFIX = '/opt/invasion-bot/'

# Load configuration
try:
    config = {
        **dotenv_values(f'{_FILE_PREFIX}.env'),
        **dotenv_values(f'{_FILE_PREFIX}.env.secret'),
        **os.environ # override .env vars with os environment vars
    }
except Exception as e:
    sys.exit(f'Failed to load configuration: {e}')

CITY_INFO = {
    "Monarch's Bluffs": {
        'search_terms': ["Monarch's Bluffs", 'bluffs', 'mb', 'monarchs', "monarch's", 'monarchbluffs'],
    },
    'Cutlass Keys': {
        'search_terms': ['Cutlass Keys', 'cutlass', 'ck', 'keys', 'cutlasskeys']
    },
    'First Light': {
        'search_terms': ['First Light', 'fl', 'firstlight']
    },
    "Weaver's Fen": {
        'search_terms': ["Weaver's Fen", 'wf', 'weavers', 'fen', 'weaversfen']
    },
    'Windsward': {
        'search_terms': ['Windsward', 'ww', 'winds']
    },
    'Mourningdale': {
        'search_terms': ['Mourningdale', 'md', 'mourning', 'morningdale']
    },
    'Reekwater': {
        'search_terms': ['Reekwater', 'rw', 'reek']
    },
    'Restless Shore': {
        'search_terms': ['Restless Shore', 'rs', 'restless', 'shores', 'restlessshore']
    },
    'Brightwood': {
        'search_terms': ['Brightwood', 'bw', 'bright']
    },
    'Everfall': {
        'search_terms': ['Everfall', 'ef', 'ever']
    },
    'Ebonscale Reach': {
        'search_terms': ['Ebonscale Reach', 'eb', 'ebonscale', 'ebons', 'reach', 'ebonscalereach']
    }
}

# Configure logging
try:
    logger = logging.getLogger(config['LOGGER_NAME'])
    logger.setLevel(logging.DEBUG)
    file_handler = logging.FileHandler(f"{_FILE_PREFIX}{config['LOG_FILE_NAME']}")
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter('%(asctime)s - %(name)-16s - %(levelname)-8s - %(message)s')
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)
except Exception as e:
    sys.exit(f"Could not initalize logger with name {config['LOGGER_NAME']}: {e}")
else:
    logger.debug('Logger initialized')

bot = commands.Bot(command_prefix='!')
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

async def get_city_name_from_term(term) -> str:
    '''Returns a string: city name match for [term] by matching it to the terms in the CITY_INFO dict'''
    logger.debug(f'Attempting to get_city_name_from_term({term})')
    for city in CITY_INFO:
        logger.debug(f'Searching {city}')
        if term in CITY_INFO[city]['search_terms']:
            logger.debug(f'Found {term} in {city}')
            return city

async def get_city_invasion_string(city, day=None) -> str:
    '''Returns a string detailing invasion status for a [city] on [day] or both today/tomorrow if [day=None](default)'''
    # verify today's invasions are actually for today, otherwise refresh data
    if CITY_INFO[city]['invasion_today_date'] != datetime.date.today().strftime('%Y-%m-%d'):
        await refresh_invasion_data()
    if day is None: # both days
        # if invasion later and it is not siege time yet
        if CITY_INFO[city]['invasion_today'] and is_siege_window_in_future(CITY_INFO[city]['siege_time']):
            response = f"{city} has an invasion later today at {CITY_INFO[city]['siege_time']} EST"
        # if invasion happened earlier today
        if CITY_INFO[city]['invasion_today'] and not is_siege_window_in_future(CITY_INFO[city]['siege_time']):
            response = f"{city} had an invasion earlier today at {CITY_INFO[city]['siege_time']} EST"
        # if invasion is tomorrow
        if CITY_INFO[city]['invasion_tomorrow']:
            response = f"{city} has an invasion tomorrow at {CITY_INFO[city]['siege_time']} EST"
        # if no invasions next two days
        if not CITY_INFO[city]['invasion_today'] and not CITY_INFO[city]['invasion_tomorrow']:
            response = f"{city} does not have any invasions today or tomorrow!"
    elif day == 'tomorrow': # tomorrow
        if CITY_INFO[city]['invasion_tomorrow']:
            response = f"{city} has an invasion tomorrow at {CITY_INFO[city]['siege_time']} EST"
        if not CITY_INFO[city]['invasion_tomorrow']:
            response = f"{city} does not have an invasion tomorrow!"
    else: # assume today otherwise
        # if invasion later and it is not siege time yet
        if CITY_INFO[city]['invasion_today'] and is_siege_window_in_future(CITY_INFO[city]['siege_time']):
            response = f"{city} has an invasion later today at {CITY_INFO[city]['siege_time']} EST"
        # if invasion happened earlier today
        if CITY_INFO[city]['invasion_today'] and not is_siege_window_in_future(CITY_INFO[city]['siege_time']):
            response = f"{city} had an invasion earlier today at {CITY_INFO[city]['siege_time']} EST"
        # if no invasion today
        if not CITY_INFO[city]['invasion_today']:
            response = f"{city} does not have an invasion today!"
    return response

async def is_siege_window_in_future(hour) -> bool:
    '''Returns a bool that is True if siege window [time] is after now'''
    logger(f'Attempting to determine if siege window is in future for: {hour}')
    if hour <1 or hour > 24 or not isinstance(hour, int):
        logger.exception(f'Cannot operate on hour: {hour}')
    time_now = datetime.datetime.now()
    time_hour = time_now.replace(hour=hour)
    return time_now < time_hour

def refresh_invasion_data(city:str = None) -> None:
    '''Gets invasion status from dynamodb for [city] or all cities if [city=None] (default)'''
    logger.debug(f'Attempting to refresh_invasion_data({city})')
    if city:
        cities_to_refresh = [city]
    else:
        cities_to_refresh = list(CITY_INFO.keys())

    for c_name in cities_to_refresh:
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
            CITY_INFO[c_name]['invasion_today'] = True
        else:
            CITY_INFO[c_name]['invasion_today'] = False
        CITY_INFO[c_name]['invasion_today_date'] = today_search_date
        logger.debug(f"Determined today's invasion status: {CITY_INFO[c_name]['invasion_today']}")
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
            CITY_INFO[c_name]['invasion_tomorrow'] = True
        else:
            CITY_INFO[c_name]['invasion_tomorrow'] = False
        CITY_INFO[c_name]['invasion_tomorrow_date'] = tomorrow_search_date
        logger.debug(f"Determined tomorrow's invasion status: {CITY_INFO[c_name]['invasion_tomorrow']}")
    logger.debug('Completed running refresh_invasion_data()')

def refresh_siege_window(city:str = None) -> None:
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

@bot.command(name='city', help='Responds with the siege window and invasion status for a city')
async def invasion(ctx, city, day=None):
    '''Responds to !city [city] [day] command with the invasion status and siege window for [city] on [day]'''
    logger.info(f'!city invoked for {city} on {day}')
    city = await get_city_name_from_term(city)
    logger.debug(f'Reformatted city name to {city}')
    # if times are valid values
    if day is None or day == 'today' or day == 'tomorrow':
        response = await get_city_invasion_string(city, day)
    else:
        response = f'Invalid response for [day], expected [today, tomorrow] got [{day}]'
    await ctx.send(response)

@bot.command(name='invasions', help='Responds with all invasions happening in the next two days')
async def all_invasions(ctx):
    '''Responds to !invasions command with all invasions happening today sorted by time'''
    logger.info('!invasions invoked')
    # refresh data if data came from a different day
    today_invasions = {}
    tomorrow_invasions = {}
    for city in CITY_INFO:
        if CITY_INFO[city]['invasion_today_date'] != datetime.date.today().strftime('%Y-%m-%d'):
            await refresh_invasion_data()
        if CITY_INFO[city]['invasion_today']:
            logger.debug(f"Found invasion today in {city} at {CITY_INFO[city]['siege_time']}")
            today_invasions[city] = f"{CITY_INFO[city]['siege_time']}"
        if CITY_INFO[city]['invasion_tomorrow']:
            logger.debug(f"Found invasion tomorrow in {city} at {CITY_INFO[city]['siege_time']}")
            tomorrow_invasions[city] = f"{CITY_INFO[city]['siege_time']}"
    logger.debug(f'Total invasions found: {str(len(today_invasions.keys()) + len(tomorrow_invasions.keys()))}')
    # this sorts today's invasions returned by their time
    sorted_partial = sorted(today_invasions, key = today_invasions.get)
    today_invasion_text = []
    for key in sorted_partial:
        today_invasion_text.append(f'{key} at {today_invasions[key]} EST')
    # this sorts tomorrow's invasions returned by their time
    sorted_partial = sorted(tomorrow_invasions, key = tomorrow_invasions.get)
    tomorrow_invasion_text = []
    for key in sorted_partial:
        tomorrow_invasion_text.append(f'{key} at {tomorrow_invasions[key]} EST')
    # determine today's response
    if len(today_invasion_text) > 2:
        today_invasion_str = ', '.join(today_invasion_text)
        today_response = f'Tonight there are {str(len(today_invasion_text))} invasions: {today_invasion_str}'
    elif len(today_invasion_text) == 2:
        today_response = f'Tonight there are 2 invasions: {today_invasion_text[0]} and {today_invasion_text[1]}'
    elif len(today_invasion_text) == 1:
        today_response = f'Tonight there is one invasion: {today_invasion_text[0]}'
    else:
        today_response = 'There are no invasions happening tonight!'
    # determine tomorrow's response
    if len(tomorrow_invasion_text) > 2:
        tomorrow_invasion_str = ', '.join(tomorrow_invasion_text)
        tomorrow_response = f'Tomorrow there are {str(len(tomorrow_invasion_text))} invasions: {tomorrow_invasion_str}'
    elif len(today_invasion_text) == 2:
        tomorrow_response = f'Tomorrow there are 2 invasions: {tomorrow_invasion_text[0]} and {tomorrow_invasion_text[1]}'
    elif len(today_invasion_text) == 1:
        tomorrow_response = f'Tomorrow there is one invasion: {tomorrow_invasion_text[0]}'
    else:
        tomorrow_response = 'There are no invasions happening tomorrow!'
    response = today_response + '\n' + tomorrow_response
    await ctx.send(response)

@bot.command(name='windows', help='Responds with all siege windows in the server')
async def windows(ctx):
    '''Respods to !windows command with a list of siege windows sorted alphabetically'''
    logger.info('!windows invoked')
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

@tasks.loop(hours = 2)
async def info_gather():
    '''Executes referesh_invasion_data and refresh_siege_window every two hours'''
    logger.info('Attempting to run scheduled task info_gather()')
    refresh_invasion_data()
    refresh_siege_window()
    logger.info('Completed running scheduled task info_gather()')

info_gather.start()
bot.run(config['DISCORD_TOKEN'])
