import datetime
import json
import logging
from typing import Optional, List, Any

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions
from importlib_resources import files

from energyhub.config import config, TIMESTAMP

DATE_RANGE_BOX_INPUT_FORMAT = '%d/%m/%Y'
DATE_FROM_BOX_INPUT_FORMAT = '%m/%d/%Y'

COOKIE_FILE_NAME = '../cache/cookies.json'
COOKIE_BASE_PATH = 'solaredgeoptimiser'
COOKIE_FILE_PATH = files(COOKIE_BASE_PATH).joinpath(COOKIE_FILE_NAME)

MONITORING_URL = 'https://monitoring.solaredge.com'

DASHBOARD_URL = f"{MONITORING_URL}/solaredge-web/p/site/{config['solar-edge-site-id']}/#/dashboard"
STORAGE_PROFILE_URL = f'{MONITORING_URL}/solaredge-web/p/home#/account/{config["solar-edge-account-id"]}/storage'

CHROMEDRIVER_PATH = "/Users/user/bin/chromedriver"
logger = logging.getLogger(__name__)


class SolarEdgeAuthenticationError(Exception):
    pass


class LoginCancelledException(Exception):
    pass


class SolarEdgeConnection:
    def __init__(self, interactive_session: bool = True):
        self.driver: Optional[webdriver.Chrome] = None
        self.interactive_session = interactive_session

    def __enter__(self):
        self.start_solar_edge_session()
        self.wait = WebDriverWait(self.driver, 10)
        return self

    def start_solar_edge_session(self):
        logger.debug('Starting browser session with solar edge')
        self.start_chrome()
        self.login_using_cached_cookies()
        logged_in = self.check_login()
        if self.interactive_session:
            while not logged_in:
                logger.info('Automatic login failed. Trying manual login.')
                try:
                    self.manual_login()
                    logged_in = self.check_login()
                except LoginCancelledException:
                    break
        if not logged_in:
            raise SolarEdgeAuthenticationError()
        self.add_cookie_consent()

    def manual_login(self):
        logger.debug('Starting manual login process')
        do_manual_login = input('Would you like to perform a manual login? [Y/n]')
        if do_manual_login.strip().lower().startswith('n'):
            logger.debug(f'Manual Login was not required (input was "{do_manual_login}"')
            raise LoginCancelledException()

        logger.debug('Showing login page')
        self.go_home()
        logger.debug('Waiting for confirmation')
        confirmation = input('Press enter when login is complete, or cancel by pressing c, Enter')
        if confirmation.strip().lower().startswith('n'):
            logger.debug(f'Manual Login was cancelled (input was "{do_manual_login}"')
            raise LoginCancelledException()
        logger.debug('Manual login confirmed')
        self.save_cookies()

    def save_cookies(self):
        logger.debug('Saving cookies')
        cookies = self.driver.get_cookies()
        filename = files(COOKIE_BASE_PATH).joinpath(TIMESTAMP + '_' + COOKIE_FILE_NAME)
        logger.debug(f'Saving to timestamped file: {filename}')
        with open(filename, 'w') as file:
            json.dump(cookies, file)
        logger.debug('Saving to cache file')
        with open(COOKIE_FILE_PATH, 'w') as file:
            json.dump(cookies, file)

    def check_login(self) -> bool:
        self.go_to_dashboard()
        try:
            site_name = WebDriverWait(self.driver, 10).until(
                expected_conditions.presence_of_element_located((By.ID, "se-siteDetailsPanel-name")))
            if not ('SolarEdge | 4 Dene Road' in self.driver.title and '4 Dene Road' in site_name.text):
                return False
        except TimeoutException:
            return False
        return True

    def go_to_dashboard(self):
        self.driver.get(DASHBOARD_URL)

    def login_using_cached_cookies(self):
        logger.debug('Reloading cached cookies')
        # visit root site to allow cookie setting (can only be set for current domain)
        self.go_home()
        with open(COOKIE_FILE_PATH, 'r') as file:
            cookies = json.load(file)
        for cookie in cookies:
            self.driver.add_cookie(cookie)

    def go_home(self):
        self.driver.get(MONITORING_URL)

    def start_chrome(self):
        options = Options()
        options.add_argument("--window-size=1920x1080")
        options.add_argument("--verbose")
        self.driver = webdriver.Chrome(options=options, executable_path=CHROMEDRIVER_PATH)

    def go_to_storage_profile(self):
        logger.debug('Loading Storage Profile page')
        self.driver.get(STORAGE_PROFILE_URL)

        profile_link = self.find_element_by_text(config["storage-profile-name"])
        logger.debug(f'Pressing link \"{config["storage-profile-name"]}\"')
        profile_link.click()

    def add_special_day(self, profile: str, date: datetime.date):
        """Assumes that we are on the storage page already..."""
        date_str = date.strftime(DATE_RANGE_BOX_INPUT_FORMAT)
        logger.info(f'Adding special day {profile} for date {date_str}')
        add_button = self.find_element_by_text('+ Add Special Day', 'button', clickable=True)
        profiles = self.get_available_profiles()
        logger.debug('Clicking Add Special Day')
        add_button.click()
        # Need to do one which waits first
        name_box = self.wait.until(expected_conditions.presence_of_element_located(
            (By.XPATH, "//div[contains(@class, 'x-window')]//input[@name='name']")))
        name_box.send_keys(date.strftime('%b %d'))
        description_box = self.driver.find_element_by_xpath(
            "//div[contains(@class, 'x-window')]//textarea[@name='description']")
        description_box.send_keys('Set by SolarEdge Optimiser')
        from_input_control = self.driver.find_element_by_name('from')
        self.set_element_value(from_input_control, date.strftime(DATE_FROM_BOX_INPUT_FORMAT))
        date_box = self.driver.find_element_by_name('dateRng')
        self.set_element_value(date_box, date_str)
        recurring_checkbox = self.driver.find_element_by_name('isRecurringCheckbox')
        # default is for recurring to be clicked so, we click it to set it off
        recurring_checkbox.click()
        profile_selector = self.driver.find_element_by_name('dailyPlan')
        # profile_index = profiles.index(profile)
        # self.set_element_value(profile_selector, profile_index)
        selector = self.driver.find_elements_by_class_name('x-form-field-trigger-wrap')
        selector[-1].click()
        profile_list = self.driver.find_elements_by_class_name('x-combo-list-item')
        wanted_profile_item = next((x for x in profile_list if x.text == profile), None)
        wanted_profile_item.click()
        # Logically the line below would be simpler as find_element and without the ancestor-or-self,
        # but that returns an element that seem not to be clickable
        create_button = self.driver.find_elements_by_xpath(
            "//button[text()='Create']/ancestor-or-self::*")[-1]
        create_button.click()
        if self.interactive_session:
            _ = input('Check the new configuration and confirm update manually. '
                      'Press Enter when Update is complete.')
        else:
            update_button = self.find_element_by_text('Update', 'button', clickable=True)
            update_button.click()
            yes_button = self.find_element_by_text('Yes', 'button', clickable=True)
            yes_button.click()

    def set_element_value(self, elem: WebElement, value: Any):
        self.driver.execute_script('''
            var elem = arguments[0];
            var value = arguments[1];
            elem.value = value;
            ''', elem, str(value))

    def get_available_profiles(self) -> List[str]:
        """Assumes that the storage page is loaded"""
        elements = self.driver.find_elements_by_xpath(
            "(//div[@class='x-fieldset-body'])[2]//u[@class='se-link']")
        profiles = [e.text for e in elements]
        return profiles

    def find_element_by_text(self, text: str, type_: str = '*', clickable: bool = False):
        locator = (By.XPATH, f"//{type_}[text()='{text}']")
        if clickable:
            condition = expected_conditions.element_to_be_clickable(locator)
        else:
            condition = expected_conditions.presence_of_element_located(locator)
        return self.wait.until(condition)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.driver.quit()

    def add_cookie_consent(self):
        cookie = json.loads("""{
                                "domain": "monitoring.solaredge.com",
                                "expiry": 2263668060,
                                "httpOnly": false,
                                "name": "solaredge_cookie_concent",
                                "path": "/",
                                "secure": false,
                                "value": "1"
                                }""")
        self.driver.add_cookie(cookie)
