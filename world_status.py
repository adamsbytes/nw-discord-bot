# world_status.py
'''Retrieves world statuses from the New World status page'''
# Disable:
#   C0301: line length (unavoidable)
# pylint: disable=C0301

from bs4 import BeautifulSoup
import requests

region_data_index_match = {
            'us-west': 0,
            'us-east': 1,
            'sa-east': 2,
            'eu-central': 3,
            'ap-southwest': 4
        }

class NWWorldStatusClient:
    '''Client to retrieve world statuses from the NW website'''
    def __init__(self, region, world) -> None:
        self.__nw_url = 'https://www.newworld.com'
        self.__nw_server_status_page_url = f'{self.__nw_url}/en-us/support/server-status'
        self.__region_id = region_data_index_match[region]
        self.world_name = world
        self.status_list = {}
        self.refresh_region_server_status()
        self.world_status = self.status_list[self.world_name]

    def has_world_status_changed(self) -> bool:
        '''Returns a bool that is true if the server status has changed'''
        has_changed = False
        self.refresh_region_server_status()
        if self.world_status != self.status_list[self.world_name]:
            has_changed = True
        self.world_status = self.status_list[self.world_name]
        return has_changed

    def refresh_region_server_status(self) -> None:
        '''Refreshes the server statuses for a given region'''
        attr_prefix = 'ags-ServerStatus-content-responses-response'
        status_page = requests.get(self.__nw_server_status_page_url)
        soup = BeautifulSoup(status_page.content, 'html.parser')
        region_results = soup.find('div', attrs={'data-index': self.__region_id})
        for world in region_results.find_all('div', attrs={'class': f'{attr_prefix}-server'}):
            world_soup = BeautifulSoup(world.prettify(), 'html.parser')
            world_name = world_soup.find('div', attrs={'class': f'{attr_prefix}-server-name'}).text.strip()
            world_status = world_soup.find('div', attrs={'class': f'{attr_prefix}-server-status'})['title']
            self.status_list[world_name] = world_status
