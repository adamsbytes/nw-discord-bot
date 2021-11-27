# discord_bot.py

from botocore.exceptions import ClientError
from discord.ext import commands, tasks
from dotenv import dotenv_values

import boto3
import datetime
import logging
import os
import sys

# Load configuration
try:
    config = {
        **dotenv_values('.env'),
        **dotenv_values('.env.secret'),
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
    file_handler = logging.FileHandler(config['LOG_FILE_NAME'])
    file_handler.setLevel(logging.DEBUG)
    file_format = logging.Formatter('%(asctime)s - %(name)-16s - %(levelname)-8s - %(message)s')
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)
except Exception as e:
    sys.exit(f'Could not initalize logger: {e}')
else:
    logger.debug('Logger initialized')

bot = commands.Bot(command_prefix='!')
try:
    if 'HOSTNAME' in os.environ: # hostname is env var on ec2, not on local dev
        logger.debug('Attempting to initialize prod Boto3 dynamodb session')
        db = boto3.client('dynamodb', region_name=config['AWS_REGION'])
    else:
        logger.debug('Attempting to initialize dev Boto3 dynamodb session')
        db = boto3.Session(profile_name=config['DEV_AWS_PROFILE']).client('dynamodb')
except Exception as e:
    logger.exception('Failed to initalize boto3 session')
else:
    logger.debug('Initialized Boto3 dynamodb session')

def get_city_name_from_term(term) -> str:
    logger.debug(f'Attempting to get_city_name_from_term({term})')
    for city in CITY_INFO:
        logger.debug(f'Searching {city}')
        if term in CITY_INFO[city]['search_terms']:
            logger.debug(f'Found {term} in {city}')
            city_name = city
            break
    return city_name

def refresh_invasion_data(city:str = None) -> None:
    logger.debug(f'Attempting to refresh_invasion_data({city})')    
    if city:
        cities_to_refresh = [city]
    else:
        cities_to_refresh = list(CITY_INFO.keys())

    for c in cities_to_refresh:
        logger.debug(f'Refreshing data in {c}')
        city_name = ''.join(e for e in c if e.isalnum()).lower()
        city_db_table = f"{config['EVENT_TABLE_PREFIX']}{city_name}"
        response = db.get_item(
            TableName=city_db_table,
            Key = {
                'date': {'S': str(datetime.date.today().strftime('%Y-%m-%d'))}
            }
        )
        logger.debug(f'Response from db: {response}')
        if 'Item' in response:
            CITY_INFO[c]['invasion_today'] = True
        else:
            CITY_INFO[c]['invasion_today'] = False
        logger.debug(f"Determined invasion status: {CITY_INFO[c]['invasion_today']}")

def refresh_siege_window(city:str = None) -> None:
    logger.debug(f'Attempting to refresh_siege_window({city})')
    table_name = config['SIEGE_INFO_TABLE_NAME']

    if city:
        cities_to_refresh = [city]
    else:
        cities_to_refresh = list(CITY_INFO.keys())

    for c in cities_to_refresh:
        logger.debug(f'Refreshing data in {c}')
        response = db.get_item(
            TableName=table_name,
            Key = {
                'city': {'S': c}
            }
        )
        CITY_INFO[c]['siege_time'] = response['Item']['time']['S']
        logger.debug(f"Determined siege time in {c}: {CITY_INFO[c]['siege_time']}")

@bot.command(name='city', help='Responds with the siege window and invasion status for a city')
async def invasion(ctx, city):
    logger.info(f'!invasion invoked for {city}')
    city = get_city_name_from_term(city)
    logger.debug(f'Reformatted city name to {city}')
    logger.debug(f"Invasion status for {city}: {str(CITY_INFO[city]['invasion_today'])}")
    if CITY_INFO[city]['invasion_today']:
        response = f"{city} has an invasion tonight. The invasion begins at {CITY_INFO[city]['siege_time']} EST"
    else:
        response = f"{city} does not have an invasion tonight. Their siege window begins at {CITY_INFO[city]['siege_time']} EST"
    await ctx.send(response)

@bot.command(name='invasions', help='Responds with all invasions happening today')
async def all_invasions(ctx):
    logger.info(f'!invasions invoked')
    invasions = {}
    for city in CITY_INFO.keys():
        if CITY_INFO[city]['invasion_today']:
            logger.debug(f"Found invasion in {city} at {CITY_INFO[city]['siege_time']}")
            invasions[city] = f"{CITY_INFO[city]['siege_time']}"
    logger.debug(f'Total invasions found: {str(len(invasions.keys()))}')
    # this sorts the invasions returned by their time
    sorted_partial = sorted(invasions, key = invasions.get)
    invasion_text = []
    print(sorted_partial)
    for key in sorted_partial:
        print(invasions[key])
        invasion_text.append(f'{key} at {invasions[key]} EST')

    if len(invasion_text) > 2:
        invasion_str = ', '.join(invasion_text)
        response = f'Tonight there are {str(len(invasion_text))} invasions: {invasion_str}'
    elif len(invasion_text) == 2:
        response = f'Tonight there are 2 invasions: {invasion_text[0]} and {invasion_text[1]}'
    elif len(invasion_text) == 1:
        response = f'Tonight there is one invasion: {invasion_text[0]}'
    else:
        response = f'There are no invasions happening tonight!'
    await ctx.send(response)

@bot.command(name='windows', help='Responds with all siege windows in the server')
async def windows(ctx):
    logger.info(f'!windows invoked')
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
    logger.info('Attempting to run scheduled task info_gather()')
    refresh_invasion_data()
    refresh_siege_window()
    logger.info('Completed running scheduled task info_gather()')

info_gather.start()
bot.run(config['DISCORD_TOKEN'])