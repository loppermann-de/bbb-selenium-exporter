#!/usr/bin/env python3

import sys, subprocess, time, logging, argparse, uuid
import pkg_resources
from PIL import Image
#from bbbutils.roomutil import RoomUtil
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.select import Select
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from prometheus_client import (
    CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST
)
from contextlib import closing, contextmanager
from tempfile import NamedTemporaryFile
from collections import namedtuple
from bigbluebutton_api_python import BigBlueButton


SELENIUM_TIMEOUT = 20
SHORT_TIMEOUT = 10
NEXT_TRY_TIMEOUT = 5
MAX_TRIES = 20


class BBBError(Exception):
    pass


def get_join_url(api, secret):
    bbb = BigBlueButton(api, secret)
    room = "selenium-exporter-{}".format(str(uuid.uuid4()))
    password = str(uuid.uuid4())
    bbb.create_meeting(room, params={'moderatorPW': password})
    return bbb.get_join_meeting_url('selenium', room, password)


def wrap_bbb_error(text):
    def outer(f):
        def inner(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as e:
                raise BBBError(text) from e
        return inner
    return outer


class BBBDriver():
    def __init__(self, api, secret, headless=True):
        chrome_options = webdriver.chrome.options.Options()
        chrome_options.add_argument("--use-fake-ui-for-media-stream")
        chrome_options.add_argument("--use-fake-device-for-media-stream")
        chrome_options.add_argument("--use-fake-device-for-audio-stream")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--no-sandbox")
        if headless:
            chrome_options.add_argument("--headless")
        self.driver = webdriver.Chrome(options=chrome_options)
        self.driver.set_script_timeout(20)
        self.driver.set_page_load_timeout(20)
        self.api = api
        self.secret = secret

    def join(self):
        bbb_url = get_join_url(self.api, self.secret)
        # open first tab
        self.driver.get(bbb_url)
        ### open sencond tab to catch poll
        self.driver.execute_script("window.open('');")
        time.sleep(2)
        with self.window(1):
            self.driver.get(bbb_url)

    def wait_clickable(self, timeout, selector):
        return WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable(selector))

    def wait_present(self, timeout, selector):
        return WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located(selector))

    def wait_visible(self, timeout, selector):
        return WebDriverWait(self.driver, timeout).until(EC.visibility_of_element_located(selector))

    def wait_invisible(self, timeout, selector):
        return WebDriverWait(self.driver, timeout).until(EC.invisibility_of_element_located(selector))

    @wrap_bbb_error('mic error')
    def enter_with_mic(self):
        self.wait_clickable(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".audioBtn--1H6rCK")).click()

    @wrap_bbb_error('no echo test error')
    def wait_for_echo_test(self):
        self.wait_clickable(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".button--1JElwW")).click()

    @wrap_bbb_error('no audio error')
    def enter_without_audio(self):
        self.wait_present(SELENIUM_TIMEOUT, (By.XPATH, "//button[@aria-label='Close Join audio modal']")).click()

    def enter_with_headphones(self):
        try:
            self.wait_clickable(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".icon-bbb-listen")).click()
        except Exception as e:
            raise BBBError('headphone error') from e

    @wrap_bbb_error('overlay error')
    def wait_for_overlays_to_disappear(self):
        self.wait_invisible(SHORT_TIMEOUT, (By.CSS_SELECTOR, ".icon-bbb-unmute"))
        self.wait_invisible(SHORT_TIMEOUT, (By.CSS_SELECTOR, ".ReactModal__Overlay"))

    @wrap_bbb_error('presentation upload error')
    def upload_presentation(self): 
        pdf_path = pkg_resources.resource_filename(__name__, 'assets/red.pdf')
        self.wait_clickable(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".button--ZzeTUF")).click()
        self.wait_visible(SELENIUM_TIMEOUT, (By.XPATH, "//span[text()='Upload a presentation']")).click()
        self.wait_visible(SELENIUM_TIMEOUT, (By.XPATH, "//input[@type='file']")).send_keys(pdf_path)
        self.wait_visible(SELENIUM_TIMEOUT, (By.XPATH, "//button[@aria-label='Upload ']")).click()
        self.wait_invisible(SELENIUM_TIMEOUT, (By.XPATH, "//button[@aria-label='Upload ']"))
        self.check_for_presentation()

    @wrap_bbb_error('video start error')
    def switch_on_video(self):
        self.wait_invisible(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".icon-bbb-unmute"))
        self.wait_invisible(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".ReactModal__Overlay"))  # FIXME: duplicate of wait_for_overlays_to_disappear
        self.wait_clickable(SELENIUM_TIMEOUT, (By.XPATH, "//button[@aria-label='Share webcam']")).click()
        Select(self.wait_present(SELENIUM_TIMEOUT, (By.ID, "setQuality"))).select_by_value('medium')
        self.wait_present(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".primary--1IbqAO > .label--Z12LMR3:nth-child(1)")).click()
        self.wait_present(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".cursorGrab--Z2fB4yK"))

    @wrap_bbb_error('chat send error')
    def send_chat_message(self):
        self.wait_clickable(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".input--2wilPX")).click()
        self.wait_clickable(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".input--2wilPX")).send_keys("hallo Chat\n")
        chat = self.wait_present(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".content--Z2nhld9"))
        assert("hallo Chat" in chat.text)

    @wrap_bbb_error('poll start error')
    def start_poll(self): 
        self.wait_clickable(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".button--ZzeTUF")).click()
        self.wait_visible(SELENIUM_TIMEOUT, (By.XPATH, "//span[text()='Start a poll']")).click()
        self.wait_present(SELENIUM_TIMEOUT, (By.XPATH, "//button[@aria-label='Yes / No']")).click()

    @wrap_bbb_error('pad enter error')
    def enter_pad(self):
        self.wait_clickable(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".listItem--Siv4F")).click()
        self.wait_present(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".note--1ESx6q"))
        self.wait_present(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".userlistPad--o5KDX"))

        for i in range(3):
            seq = self.wait_present(SELENIUM_TIMEOUT, (By.TAG_NAME, "iframe"))
            iframe = self.driver.find_elements_by_tag_name('iframe')[0]
            self.driver.switch_to.frame(iframe)

    @wrap_bbb_error('pad edit error')
    def edit_etherpad(self):
        self.enter_pad()
        self.wait_present(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".ace-line")).send_keys("hallo Pad\n")

    @wrap_bbb_error('poll error')
    def check_for_poll(self):
        self.wait_present(SHORT_TIMEOUT, (By.XPATH, "//button[@aria-label='Yes']")).click()

    def check_for_presentation(self):
        return self.wait_screenshot_pixel(
                (By.CSS_SELECTOR, ".svgContainer--Z1z3wO0"),
                lambda pixels, pres: pixels[pres.size['width'] / 2, pres.size['height'] / 2],
                lambda pixel: pixel[0] > 200 and pixel[1] < 50 and pixel[2] < 50)

    def check_for_video(self):
        return self.wait_screenshot_pixel(
                (By.CSS_SELECTOR, ".cursorGrab--Z2fB4yK"),
                lambda pixels, _: pixels[2, 20],
                lambda pixel: pixel[0] < 50 and pixel[1] > 70 and pixel[2] < 50)

    def wait_screenshot_pixel(self, selector, get_pixel, pixel_ok, max_tries=MAX_TRIES):
        for i in range(max_tries):
            time.sleep(1)
            try:
                element = self.wait_present(1, selector)
            except:
                continue
            with NamedTemporaryFile(suffix='.png') as tmp:
                element.screenshot(tmp.name)
                pixels = Image.open(tmp.name).load()
            pixel = get_pixel(pixels, element)
            if pixel_ok(pixel):
                return

        raise TimeoutError("max tries exceeded")

    @wrap_bbb_error('chat message not found')
    def check_for_chat_message(self):
        chat = self.wait_present(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".content--Z2nhld9"))
        assert("hallo Chat" in chat.text)

    @wrap_bbb_error('etherpad line not found')
    def check_for_etherpad(self):
        self.enter_pad()
        pad = self.wait_present(SELENIUM_TIMEOUT, (By.CSS_SELECTOR, ".ace-line"))
        assert("hallo Pad" in pad.text)

    @contextmanager
    def window(self, num):
        try:
            self.driver.switch_to.window(self.driver.window_handles[num])
            yield None
        finally:
            self.driver.switch_to.window(self.driver.window_handles[0])

    def close(self):
        self.driver.quit()


Gauges = namedtuple('Gauges', ['success', 'duration'])


def bbb_scenario(gauges):
    def wrapper(f):
        def inner(*args, **kwargs):
            with gauges.duration.time():
                try:
                    f(*args, **kwargs)
                    gauges.success.set(True)
                    return True
                except Exception as e:
                    logging.error(e)
                    return False
        return inner
    return wrapper


def bbb_connection(hostname, secret, headless=True):
    return BBBDriver(f'https://{hostname}/bigbluebutton/', secret, headless=headless)


def collect(hostname, secret, headless=True):
    registry = CollectorRegistry(auto_describe=True)
    
    labelnames = ['backend']
    labelvalues = (hostname)

    def make_gauges(slug, description):
        success = Gauge(f'{slug}_success', f'Success of {description}', labelnames, registry=registry).labels(labelvalues)
        success.set(False)
        duration = Gauge(f'{slug}_duration_seconds', f'Duration of {description}', labelnames, registry=registry).labels(labelvalues)
        duration.set(0)
        return Gauges(success, duration)

    @bbb_scenario(make_gauges('connect_server', 'connecting to BBB server'))
    def connect_server(conn):
        conn.join()

    @bbb_scenario(make_gauges('echo_test', 'waiting for echo test'))
    def echo_test(conn):
        conn.enter_with_mic()
        conn.wait_for_echo_test()

    @bbb_scenario(make_gauges('join_headphone', 'joining room with headphones'))
    def join_headphone(conn):
        with conn.window(1):
            conn.enter_with_headphones()

    @bbb_scenario(make_gauges('start_cam', 'starting camera'))
    def start_cam(conn):
        conn.wait_for_overlays_to_disappear()
        conn.switch_on_video()
        with conn.window(1):
            conn.check_for_video()

    @bbb_scenario(make_gauges('upload_pres', 'uploading presentation'))
    def upload_pres(conn):
        conn.upload_presentation()

    @bbb_scenario(make_gauges('chat_test', 'testing chat'))
    def chat_test(conn):
        conn.send_chat_message()
        with conn.window(1):
            conn.check_for_chat_message()

    @bbb_scenario(make_gauges('poll_test', 'testing poll'))
    def poll_test(conn):
        conn.start_poll()
        with conn.window(1):
            conn.check_for_poll()

    @bbb_scenario(make_gauges('etherpad_test', 'testing etherpad'))
    def etherpad_test(conn):
        conn.edit_etherpad()
        with conn.window(1):
            conn.check_for_etherpad()


    with closing(bbb_connection(hostname, secret, headless=headless)) as conn:
        if not connect_server(conn):
            return registry
        
        if not echo_test(conn):
            conn.enter_without_audio()

        join_headphone(conn)
        start_cam(conn)
        upload_pres(conn)
        chat_test(conn)
        poll_test(conn)
        etherpad_test(conn)

    return registry
