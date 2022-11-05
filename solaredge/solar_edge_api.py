import calendar
import datetime
import inspect
import json
import logging
import os
import pathlib
from typing import Dict, Tuple

import requests

from energyhub.config import config

API_URL = 'https://monitoringapi.solaredge.com'
API_DATE_FORMAT = "%Y-%m-%d"
API_TIME_FORMAT = (API_DATE_FORMAT + " %H:%M:%S")
logger = logging.getLogger(__name__)
CACHEDIR = pathlib.Path(os.path.dirname(inspect.getsourcefile(lambda: 0))) / '..' / 'cache/'


class BatteryNotFoundError(Exception):
    pass


def get_power_history_for_site():
    site_start_date, site_end_date = get_site_dates()
    start_date = site_start_date
    end_date = _end_of_month(start_date)
    while start_date < site_end_date:
        data = get_power_details(start_date, end_date)
        month_label = start_date.strftime('%Y-%m')
        with open(f'power_details_{month_label}.json', 'w') as file:
            json.dump(data, file, indent=4)
        start_date = _start_of_next_month(start_date)
        end_date = _end_of_month(start_date)


def get_battery_history_for_site():
    site_start_date, site_end_date = get_site_dates()
    one_week = datetime.timedelta(days=7)
    start_date = site_start_date
    end_date = start_date + one_week - datetime.timedelta(hours=1)
    while start_date < site_end_date:
        # loop over weeks
        battery_data = api_request('storageData', {'startTime': start_date, 'endTime': end_date})
        with open(f'battery_details_{start_date.strftime(API_DATE_FORMAT)}.json', 'w') as file:
            json.dump(battery_data, file, indent=4)
        start_date = start_date + one_week
        end_date = end_date + one_week


def _start_of_next_month(date):
    if date.month == 12:
        new_date = date.replace(year=date.year+1, month=1, day=1, hour=0, minute=0, second=0)
    else:
        new_date = date.replace(month=date.month + 1, day=1, hour=0, minute=0, second=0)
    return new_date


def _end_of_month(start_date):
    _, last_day_of_month = calendar.monthrange(start_date.year, start_date.month)
    end_of_month = start_date.replace(day=last_day_of_month)
    end_of_month = end_of_month.combine(end_of_month.date(), datetime.time(23, 59))
    return end_of_month


def get_site_dates() -> Tuple[datetime.datetime, datetime.datetime]:
    date_range_data = api_request('dataPeriod')
    start_date = datetime.datetime.strptime(date_range_data['dataPeriod']['startDate'], API_DATE_FORMAT)
    end_date = datetime.datetime.strptime(date_range_data['dataPeriod']['endDate'], API_DATE_FORMAT)
    return start_date, end_date


def get_power_details(start_time: datetime.datetime, end_time: datetime.datetime):
    params = {'startTime': start_time, 'endTime': end_time}
    data = api_request('powerDetails', params)
    logger.debug(json.dumps(data, indent=4))
    return data


def get_power_flow():
    data = api_request('currentPowerFlow')
    logger.debug(data)
    return data['siteCurrentPowerFlow']


def get_battery_level():
    logger.debug('Getting battery level')
    start_time = datetime.datetime.now() - datetime.timedelta(minutes=60)
    end_time = datetime.datetime.now() + datetime.timedelta(minutes=15)
    params = {'startTime': start_time, 'endTime': end_time}
    data = api_request('storageData', params)
    logger.debug(data)
    n_batteries = data['storageData']['batteryCount']
    if n_batteries != 1:
        msg = f'Expected 1 battery, but found {n_batteries}'
        logger.error(msg)
        raise BatteryNotFoundError(msg)
    battery_data = data['storageData']['batteries'][0]
    if battery_data['telemetryCount'] == 0:
        msg = f'No telemetry data found'
        logger.error(msg)
        raise BatteryNotFoundError(msg)
    charge = battery_data['telemetries'][-1]['batteryPercentageState']
    logger.debug(f'Battery charge is {charge}')
    return charge


def _format_if_datetime(value):
    if isinstance(value, datetime.datetime):
        return value.strftime(API_TIME_FORMAT)


def api_request(function: str, params: dict = None) -> Dict:
    if params is None:
        params = {}
    params = {key: _format_if_datetime(value) for key, value in params.items()}
    params['api_key'] = config['solar-edge-api-key']
    url = '/'.join((API_URL, 'site', str(config['solar-edge-site-id']), function))
    response = requests.get(url, params=params)
    response.raise_for_status()
    return json.loads(response.text)
