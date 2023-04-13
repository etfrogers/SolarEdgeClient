import calendar
import datetime
import inspect
import json
import logging
import os
import pathlib
from typing import Dict, Tuple, List, Union

import numpy as np
import requests

from energyhub.config import config

API_URL = 'https://monitoringapi.solaredge.com'
API_DATE_FORMAT = "%Y-%m-%d"
API_TIME_FORMAT = (API_DATE_FORMAT + " %H:%M:%S")
logger = logging.getLogger(__name__)
CACHEDIR = pathlib.Path(os.path.dirname(inspect.getsourcefile(lambda: 0))) / '..' / 'cache/'


class BatteryNotFoundError(Exception):
    pass


class SolarEdgeClient:
    def __init__(self, api_key, site_id, timezone=None):
        self.api_key = api_key
        self.site_id = site_id
        # Note all times are returned in the timezone of the site
        self.timezone = timezone

    def api_request(self, function: str, params: dict = None) -> Dict:
        if params is None:
            params = {}
        params = {key: _format_if_datetime(value) for key, value in params.items()}
        params['api_key'] = self.api_key
        url = '/'.join((API_URL, 'site', str(self.site_id), function))
        response = requests.get(url, params=params)
        response.raise_for_status()
        return json.loads(response.text)

    def get_power_flow(self):
        data = self.api_request('currentPowerFlow')
        logger.debug(data)
        data = data['siteCurrentPowerFlow']
        error_power = round(2**15 / 1000, 2)
        if data['STORAGE']['currentPower'] == error_power:
            data['STORAGE']['currentPower'] = 0
            data['LOAD']['currentPower'] -= error_power
        return data

    def get_energy_for_day(self, date: datetime.date) -> Dict[str, float]:
        output = self.get_energy_details(*day_start_end_times(date))
        assert output['timestamps'].date() == date
        return output

    def get_power_history_for_day(self, date: datetime.date) -> Dict[str, np.ndarray]:
        data = self.get_power_details(*day_start_end_times(date),
                                      time_unit='QUARTER_OF_AN_HOUR')
        details = data['powerDetails']
        assert details['timeUnit'] == 'QUARTER_OF_AN_HOUR'
        assert details['unit'] == 'W'
        output = self.meter_list_to_dict(details['meters'])
        return output

    def meter_list_to_dict(self, meters: List) -> Dict[str, Union[float, np.ndarray]]:
        timestamp_list = self._extract_time_stamps(meters[0]['values'], 'date')
        single_entry = len(timestamp_list) == 1
        if single_entry:
            timestamps = timestamp_list[0]
        else:
            timestamps = np.array(timestamp_list)
        output = {'timestamps': timestamps}
        for meter_data in meters:
            meter_name = meter_data['type']
            values = meter_data['values']
            assert self._extract_time_stamps(values, 'date') == timestamp_list
            if single_entry:
                powers = values[0]['value']
            else:
                powers = np.array([entry.get('value', 0) for entry in values])
            output[meter_name] = powers
        return output

    def _extract_time_stamps(self, value_list: List[dict], time_name: str) -> List[datetime.datetime]:
        times = [datetime.datetime.strptime(entry[time_name], API_TIME_FORMAT) for entry in value_list]
        times = [t.replace(tzinfo=self.timezone) for t in times]
        return times

    def get_power_details(self, start_time: datetime.datetime, end_time: datetime.datetime,
                          time_unit: str = 'DAY'):
        params = {'startTime': start_time, 'endTime': end_time, 'timeUnit': time_unit}
        data = self.api_request('powerDetails', params)
        logger.debug(json.dumps(data, indent=4))
        return data

    def get_energy_details(self, start_time: datetime.datetime, end_time: datetime.datetime,
                           time_unit: str = 'DAY'):
        params = {'startTime': start_time, 'endTime': end_time, 'timeUnit': time_unit}
        data = self.api_request('energyDetails', params)
        logger.debug(json.dumps(data, indent=4))
        data = data['energyDetails']
        assert data['unit'] == 'Wh'
        data = self.meter_list_to_dict(data['meters'])
        return data

    def get_site_dates(self) -> Tuple[datetime.datetime, datetime.datetime]:
        date_range_data = self.api_request('dataPeriod')
        start_date = datetime.datetime.strptime(date_range_data['dataPeriod']['startDate'], API_DATE_FORMAT)
        end_date = datetime.datetime.strptime(date_range_data['dataPeriod']['endDate'], API_DATE_FORMAT)
        return start_date, end_date

    def get_power_history_for_site(self):
        site_start_date, site_end_date = self.get_site_dates()
        start_date = site_start_date
        end_date = _end_of_month(start_date)
        while start_date < site_end_date:
            data = self.get_power_details(start_date, end_date)
            month_label = start_date.strftime('%Y-%m')
            with open(f'power_details_{month_label}.json', 'w') as file:
                json.dump(data, file, indent=4)
            start_date = _start_of_next_month(start_date)
            end_date = _end_of_month(start_date)

    def get_battery_history_for_day(self, date: datetime.date):
        data = self.get_battery_history(*day_start_end_times(date))
        data = data['storageData']
        if data['batteryCount'] != 1:
            raise NotImplementedError
        data = data['batteries'][0]
        timestamp_list = self._extract_time_stamps(data['telemetries'], 'timeStamp')
        charge_power_from_grid = [entry['power']
                                  if (entry['power'] is not None
                                      and entry['power'] > 0
                                      and entry['ACGridCharging'] > 0) else 0
                                  for entry in data['telemetries']]
        charge_power_from_solar = [entry['power']
                                   if (entry['power'] is not None
                                       and entry['power'] > 0
                                       and entry['ACGridCharging'] == 0) else 0
                                   for entry in data['telemetries']]
        discharge_power = [-entry['power']
                           if (entry['power'] is not None and entry['power'] < 0) else 0
                           for entry in data['telemetries']]
        charge_percentage = [entry['batteryPercentageState'] for entry in data['telemetries']]
        charge_percentage = np.array(charge_percentage)
        full_charge_energy = [entry['fullPackEnergyAvailable'] for entry in data['telemetries']]
        full_charge_energy = np.array(full_charge_energy)
        energy_stored = full_charge_energy * charge_percentage / 100
        timestamps = np.array(timestamp_list)
        output = {'timestamps': timestamps,
                  'charge_power_from_grid': np.array(charge_power_from_grid),
                  'discharge_power': np.array(discharge_power),
                  'charge_power_from_solar': np.array(charge_power_from_solar),
                  'charge_percentage': np.asarray(charge_percentage),
                  'charge_from_grid_energy': sum([entry['ACGridCharging'] for entry in data['telemetries']]),
                  'discharge_energy': self.integrate_power(timestamps, discharge_power),
                  'charge_from_solar_energy': self.integrate_power(timestamps, charge_power_from_solar),
                  'stored_energy': energy_stored
                  }
        return output

    @staticmethod
    def integrate_power(timestamps, powers):
        dt = np.diff(timestamps)
        # assume first entry is the standard 5-minute interval.
        dt = np.concatenate(([datetime.timedelta(minutes=5)], dt))
        dt_seconds = np.array([t.total_seconds() for t in dt])
        dt_hours = dt_seconds / (60 * 60)
        dt_hours = dt_hours
        return np.sum(dt_hours * powers)

    def get_battery_history(self, start_date: datetime.datetime, end_date: datetime.datetime):
        battery_data = self.api_request('storageData',
                                        {'startTime': start_date, 'endTime': end_date})
        return battery_data

    def get_battery_history_for_site(self):
        site_start_date, site_end_date = self.get_site_dates()
        one_week = datetime.timedelta(days=7)
        start_date = site_start_date
        end_date = start_date + one_week - datetime.timedelta(hours=1)
        while start_date < site_end_date:
            # loop over weeks
            battery_data = self.get_battery_history(start_date, end_date)
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


def _format_if_datetime(value):
    if isinstance(value, datetime.datetime):
        return value.strftime(API_TIME_FORMAT)
    else:
        return value


def day_start_end_times(day: datetime.date):
    start = datetime.datetime.combine(day, datetime.time(0), tzinfo=config.timezone)
    # period is inclusive, so if we don't subtract one second, we ge the frst period of the next day too.
    end = start + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    return start, end
