import datetime
import functools
import json
import re
from glob import glob
from typing import List, Sequence

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
from matplotlib.ticker import FuncFormatter
from numpy_groupies import aggregate
from scipy.interpolate import interp1d

from yrpy.astro_data import get_sun_data
from solaredge.solar_edge_api import get_power_history_for_site, API_TIME_FORMAT, get_battery_history_for_site
from solaredge.solar_edge_api import CACHEDIR

# noinspection PyUnresolvedReferences
class PowerHistory:
    meters = ['consumption', 'production', 'feed_in', 'purchased']  # , 'self_consumption']

    def __init__(self, get_from_server: bool = False):
        self._timestamp_list = []
        self._battery_timestamp_list = []
        self._battery_power_list = []
        self._battery_power = np.array([])
        self._battery_power_ungrouped_list = []
        for meter in self.meters:
            setattr(self, self._list_name(meter), [])
        self.load_power_history(get_from_server)
        self.load_battery_history(get_from_server)
        self.group_battery_powers()

    def load_power_history(self, get_from_server: bool = False) -> None:
        if get_from_server:
            get_power_history_for_site()
        history_files = glob(str(CACHEDIR / 'power_details_*.json'))
        for filename in history_files:
            with open(filename) as file:
                file_data = json.load(file)
            details = file_data['powerDetails']
            assert details['timeUnit'] == 'QUARTER_OF_AN_HOUR'
            assert details['unit'] == 'W'
            timestamp_list = self._extract_time_stamps(details['meters'][0]['values'], 'date')
            self._timestamp_list.extend(timestamp_list)
            for meter_data in details['meters']:
                meter_name = meter_data['type']
                values = meter_data['values']
                assert self._extract_time_stamps(values, 'date') == timestamp_list
                powers = [entry.get('value', 0) for entry in values]
                list_ = self._get_list(meter_name)
                list_.extend(powers)

        indices = argsort(self._timestamp_list)
        self._timestamp_list = list_indexed_by_list(self._timestamp_list, indices)
        for list_ in self._meter_list_names():
            sorted_list = list_indexed_by_list(getattr(self, list_), indices)
            setattr(self, list_, sorted_list)

    def load_battery_history(self, get_from_server: bool = False) -> None:
        if get_from_server:
            get_battery_history_for_site()
        history_files = glob(str(CACHEDIR / 'battery_details_*.json'))
        for filename in history_files:
            with open(filename) as file:
                file_data = json.load(file)
            storage_data = file_data['storageData']
            assert storage_data['batteryCount'] == 1
            telemetry_list = storage_data['batteries'][0]['telemetries']
            timestamps = self._extract_time_stamps(telemetry_list, 'timeStamp')
            powers = [t['power'] for t in telemetry_list]
            # Convert None to 0
            powers = [0. if p is None else p for p in powers]
            self._battery_timestamp_list.extend(timestamps)
            self._battery_power_list.extend(powers)

        indices = argsort(self._battery_timestamp_list)
        self._battery_timestamp_list = list_indexed_by_list(self._battery_timestamp_list, indices)
        self._battery_power_list = list_indexed_by_list(self._battery_power_list, indices)

    def group_battery_powers(self):
        self._battery_power_ungrouped_list = self._battery_power_list.copy()
        numeric_timestamps = np.array([d.timestamp() for d in self._timestamp_list])
        interpolator = interp1d(numeric_timestamps,
                                np.arange(self.timestamps.size),
                                kind='previous')
        battery_numeric_timestamps = np.array([d.timestamp() for d in self._battery_timestamp_list])
        group_indices = interpolator(battery_numeric_timestamps).astype(int)
        sorted_indices = np.unique(group_indices)
        sorted_indices.sort()
        # battery_grouped_timestamps = self.timestamps[sorted_indices]
        battery_grouped_power = aggregate(group_indices, self.battery_power_ungrouped, func='mean')
        battery_power = np.zeros_like(self.production)
        battery_power[sorted_indices] = battery_grouped_power[sorted_indices]
        self._battery_power = battery_power

    @functools.cached_property
    def timestamps(self):
        return np.array(self._timestamp_list)

    @property
    def solar_production(self):
        return self.production + self.battery_power

    @property
    def battery_power(self) -> np.ndarray:
        return self._battery_power

    @functools.cached_property
    def battery_power_ungrouped(self) -> np.ndarray:
        return np.array(self._battery_power_ungrouped_list)

    @functools.cached_property
    def battery_timestamps(self) -> np.ndarray:
        return np.array(self._battery_timestamp_list)

    @functools.cached_property
    def is_battery_charging(self) -> np.ndarray:
        return self.battery_power > 0

    @functools.cached_property
    def battery_charge_rate(self) -> np.ndarray:
        charging = self.battery_power.copy()
        charging[~self.is_battery_charging] = 0
        return charging

    @functools.cached_property
    def battery_production(self) -> np.ndarray:
        production = self.battery_power.copy()
        production[self.is_battery_charging] = 0
        production = -production
        return production

    @functools.cached_property
    def times(self):
        return np.array([xi.replace(year=2021, month=1, day=1) for xi in self.timestamps])

    @functools.cached_property
    def dates(self):
        return np.array([xi.date() for xi in self.timestamps])

    @staticmethod
    def _extract_time_stamps(value_list: List[dict], time_name: str) -> List[datetime.datetime]:
        times = [datetime.datetime.strptime(entry[time_name], API_TIME_FORMAT) for entry in value_list]
        return times

    def _list_to_array(self, meter: str) -> np.ndarray:
        return np.array(getattr(self, self._list_name(meter)))

    def _meter_list_names(self):
        for meter in self.meters:
            yield self._list_name(meter)

    @staticmethod
    def _list_name(meter_name: str) -> str:
        return f'_{_camel_to_snake(meter_name)}_list'

    def _get_list(self, meter_name: str) -> List:
        return getattr(self, self._list_name(meter_name))

    def plot_production(self) -> None:
        time = datetime.datetime(2021, 12, 25)
        indices = ((time < self.timestamps)
                   & (self.timestamps < (time + datetime.timedelta(days=1))))
        times = self.timestamps[indices]
        plt.figure()
        ax1 = plt.subplot(3, 1, 1)
        plt.plot(times, self.solar_production[indices], label='Solar Production')
        plt.plot(times, self.feed_in[indices], label='Export')
        plt.plot(times, self.purchased[indices], label='Import')
        plt.plot(times, self.battery_production[indices], label='Battery production')
        plt.plot(times, self.battery_charge_rate[indices], label='Battery Charging')
        # ax1.set_xlim([time, time + datetime.timedelta(days=1)])
        plt.legend(loc='upper left')

        ax2 = plt.subplot(3, 1, 2)
        plt.plot(times, self.consumption[indices], label='Consumption')
        # plt.plot(times, self.self_consumption[indices], label='Self Consumption')
        plt.legend()
        ax3 = plt.subplot(3, 1, 3)
        plt.plot(times, self.battery_production[indices], label='Battery production')
        plt.plot(times, self.battery_charge_rate[indices], label='Battery charging')
        plt.plot(times, self.production[indices], label='System production')
        plt.plot(times, self._battery_power[indices], label='Battery power')
        plt.plot(self._battery_timestamp_list, self.battery_power_ungrouped,
                 label='Raw Battery power', linestyle='--')
        plt.legend(loc='upper left')
        ax3.set_xlim([time, time + datetime.timedelta(days=1)])

        ax1.xaxis.set_major_formatter(self._time_format())
        ax2.xaxis.set_major_formatter(self._time_format())
        ax3.xaxis.set_major_formatter(self._time_format())

        plt.show()

    def plot_solar_waterfall(self, adjust_for_sunrise: bool = True):
        plt.figure()
        plot_args = {'alpha': 0.2, 'linestyle': None, 'marker': '.'}
        # General for BST. could be adjusted per day later if needed
        # Note: all times are forced to be on 1/1/2021
        # They need to be datetime not time objects to plot
        if adjust_for_sunrise:
            times = self.timestamps
            sun_data = [get_sun_data(d) for d in self.dates]
            noons = np.array([day.solar_noon for day in sun_data])
            sunrises = np.array([day.sunrise for day in sun_data])
            sunsets = np.array([day.sunset for day in sun_data])
            am_indices = times < noons
            am_times = times[am_indices]
            pm_times = times[~am_indices]
            am_production = self.solar_production[am_indices]
            pm_production = self.solar_production[~am_indices]
            am_seconds_axis = [td.total_seconds() for td in (am_times-sunrises[am_indices])]
            pm_seconds_axis = [td.total_seconds() for td in (pm_times-sunsets[~am_indices])]
            plt.subplot(2, 1, 1)
            plt.plot(am_seconds_axis, am_production, **plot_args)
            plt.gca().xaxis.set_major_formatter(FuncFormatter(self._timedelta_format))
            plt.subplot(2, 1, 2)
            plt.plot(pm_seconds_axis, pm_production, **plot_args)
            plt.gca().xaxis.set_major_formatter(FuncFormatter(self._timedelta_format))

        else:
            plt.plot(self.times, self.solar_production, **plot_args)
            plt.gca().xaxis.set_major_formatter(self._time_format())
        plt.show()

    @staticmethod
    def _time_format():
        return DateFormatter("%H:%M")

    @staticmethod
    def _timedelta_format(x, pos):
        hours = int(x // 3600)
        minutes = int((x % 3600) // 60)
        # seconds = int(x%60)

        return "{:d}:{:02d}".format(hours, minutes)
        # return "{:d}:{:02d}:{:02d}".format(hours, minutes, seconds)


for meter_ in PowerHistory.meters:
    # noinspection PyProtectedMember
    setattr(PowerHistory, meter_, property(fget=functools.partial(PowerHistory._list_to_array, meter=meter_)))


def _camel_to_snake(name: str) -> str:
    name = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
    return re.sub('([a-z0-9])([A-Z])', r'\1_\2', name).lower()


def argsort(seq: Sequence) -> Sequence:
    # http://stackoverflow.com/questions/3071415/efficient-method-to-calculate-the-rank-vector-of-a-list-in-python
    return sorted(range(len(seq)), key=seq.__getitem__)


def list_indexed_by_list(lst: List, indices: List[int]) -> List:
    return [lst[i] for i in indices]


def main():
    history = PowerHistory(get_from_server=False)
    history.plot_production()
    history.plot_solar_waterfall()


if __name__ == '__main__':
    main()
