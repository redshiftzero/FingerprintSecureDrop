#!/usr/bin/env python

# Automates visiting sites using Tor Browser running in a virtual X11
# framebuffer. Does not take an arbitrary list of sites, but rather parses a
# logfile created by running ./sort_hidden_services.py wherein sets of
# up-and-running hidden services sorted by whether they are SecureDrop or not
# can be found. Arguments found in the imports/ globals block starting with
# `import configparse` just below can be set via the ./config.ini file.

import re
from getpass import getpass
from time import sleep
from sys import exit
from os import environ, getcwd
from os.path import join, expanduser
from datetime import datetime as dt
import configparser
import pickle

from utils import setup_logging, timestamp

import stem
from stem.process import launch_tor
from stem.control import Controller

from site import addsitedir
addsitedir(join(getcwd(), 'tor-browser-selenium'))
from tbselenium.tbdriver import TorBrowserDriver
from tbselenium.common import USE_RUNNING_TOR
from tbselenium.utils import start_xvfb, stop_xvfb
import selenium.common.exceptions

class Crawler:
    def __init__(self):
        logger.info('Parsing config.ini...')
        self.parse_config()
        logger.info('Unpickling onions...')
        with open(self.class_data, 'rb') as pj:
            self.sds = pickle.load(pj)
            self.not_sds = pickle.load(pj)
        self.sds_ord = len(self.sds)
        self.not_sds_ord = len(self.not_sds)
        self.chunk_ord = int(self.not_sds_ord / self.ratio)

    def parse_config(self):
        config = configparser.ConfigParser()
        config.read('config.ini')
        self.sec = config['Crawl Hidden Services']
        home_path = expanduser('~')
        self.tbb_path = join(home_path, self.sec['tbb_path'])
        fpsd_path = join(home_path, self.sec['fpsd_path'])
        self.log_path = join(fpsd_path, 'logging')
        self.tbb_logfile_path = join(self.log_path, self.sec['tbb_logfile'])
        self.torrc_path = self.sec['torrc_path']
        self.socks_port = int(self.sec['socks_port'])
        self.control_port = int(self.sec['control_port'])
        self.class_data = join(self.log_path, self.sec['class_data'])
        self.p_load_to = int(self.sec['page_load_timeout'])
        self.take_screenshots = self.sec.getboolean('take_screenshots')
        self.ratio = int(self.sec['sd_sample_ratio'])

    def crawl_classes(self):

        logger.info('Creating a virtual framebuffer...')
        virt_framebuffer = start_xvfb()

        logger.info('Starting tor with stem...')
        launch_tor(torrc_path=self.torrc_path, take_ownership=True)

        logger.info('Attempting authenticate to control port...')
        try:
            self.controller = Controller.from_port(port=self.control_port)
        except stem.SocketError as exc:
            print('Unable to connect to tor on port {}: {}'.format(self.control_port,
                                                                   exc))
            exit(1)
        try:
            self.controller.authenticate()
        except stem.connection.MissingPassword:
            # The user has a passwd instead of CookieAuth set in their torrc,
            # so we'll prompt for it
            pw = getpass("Controller password: ")
            try:
                self.controller.authenticate(password = pw)
            except stem.connection.PasswordAuthFailed:
                print('Unable to authenticate, password is incorrect')
                exit(1)
        except stem.connection.AuthenticationFailure as exc:
            print('Unable to authenticate: {}'.format(exc))
            exit(1)


        logger.info('Starting Tor Browser in virtual framebuffer...')
        with TorBrowserDriver(tbb_path = self.tbb_path,
                              tbb_logfile_path = self.tbb_logfile_path,
                              tor_cfg = USE_RUNNING_TOR) as driver:

            # Set maximum time to wait for a page to load (default 15s)
            driver.set_page_load_timeout(self.p_load_to)

            # Open the Tor cell log
            cell_log = open(self.tor_cell_log, 'r')

            # We intersperse our sd_sample_ratio crawls of the SD class between
            # our singular crawl of the non-SD class
            for i in range(self.ratio):

                logger.info('Crawling the SecureDrop class...')
                self.crawl_class(self.sds, driver)

                chunk = self.not_sds[i * self.chunk_ord :
                                     max((i + 1) * self.chunk_ord,
                                        self.not_sds_ord)]
                logger.info('Crawling part {}/{} of non-SDs...'.format(i+1,
                                                                      self.ratio))
                self.crawl_class(chunk, driver)


        logger.info('Crawling has succesfully completed!')
        logger.info('Stopping the virtual framebuffer...')
        stop_xvfb(virt_framebuffer)
        logger.info('Program successfully exiting...')

    def crawl_class(self, wf_class, driver):
        for url in wf_class:
            try:
                try:
                    logger.info('{}: loading...'.format(url))
                    driver.get(url)
                except selenium.common.exceptions.TimeoutException:
                    tw = '{}: timed out after {}s'.format(url, self.p_load_to)
                    logger.warning(tw)
                    continue

                # See if we've hit a connection error page
                if driver.is_connection_error_page:
                    logger.warning('{}: connection error page'.format(url))
                    continue

                # Take a screenshot of the page (def. false)
                if self.take_screenshots:
                    logger.info('{}: capturing screen...')
                    try:
                        img_fn = '{}_{}.png'.format(url[7:-6], timestamp())
                        img_path = join(self.log_path, img_fn)
                        driver.get_screenshot_as_file(img_path)
                    except selenium.common.exceptions.WebDriverException:
                        logger.warning('{}: screenshot failed'.format(url))
                        continue

            # Catch unusual exceptions and log them
            except Exception as exc:
                    logger.warning('{}: exception: {}'.format(url, exc))
                    continue

            # Sleep 5s to catch traffic after initial onload event
            sleep(5)

            # Close all open circuits. Since we're only working w/ HSs, which
            # Tor uses a different circuit for each time 
            for circuit in self.controller.get_circuits(): # if circuit != 
                self.controller.close_circuit(circuit.id)

            logger.info('{}: succesfully loaded'.format(url))

if __name__ == '__main__':
    # ./logging/crawler-log-latest.txt will be a symlink to the latest log,
    # which will be timestamped and also in the ./logging folder
    logger = setup_logging('crawler')
    # Initialize crawler w/ the options defined in our config.ini file
    logger.info('Initializing crawler...')
    crawler = Crawler()
    # Crawl the SD and non-SD classes sorted by ./sort_hidden_services.py
    # defined in the hs_sorter_logfile as defined in config.ini
    crawler.crawl_classes()
