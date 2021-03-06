#!/usr/bin/env python3.5
#
# Automates visiting onion services using Tor Browser in a virtual framebuffer
# and logs Tor cell traces from them to either a PostgreSQL database or
# plaintext files.

from ast import literal_eval
import codecs
import http.client
from io import SEEK_END, SEEK_SET
from os import mkdir
from os.path import abspath, dirname, join
import pickle
import platform
import random
import re
from selenium.common.exceptions import WebDriverException, TimeoutException
import stem
from stem.process import launch_tor_with_config
from stem.control import Controller
from sys import exc_info
from time import sleep
from traceback import format_exception
import urllib.parse
from urllib.request import urlopen
from tbselenium.tbdriver import TorBrowserDriver
from tbselenium.common import USE_RUNNING_TOR
from tbselenium.utils import start_xvfb, stop_xvfb

from database import RawStorage
from utils import (find_free_port, get_config, get_timestamp, panic,
                   setup_logging, symlink_cur_to_latest, timestamp_file)
from version import __version__ as _version

_dir = dirname(abspath(__file__))
_log_dir = join(_dir, "logging")

# The Crawler handles most errors internally so the user does not have to worry
# about exception handling
class CrawlerException(Exception):
    """Base class for Crawler exceptions."""

class CrawlerLoggedError(CrawlerException):
    """Raised when a crawler encounters and logs a boring exception."""

class CrawlerReachedErrorPage(CrawlerLoggedError):
    """Raised when the crawler reaches an error page or the about:newtab
    page"""

class CrawlerNoRendCircError(CrawlerException):
    """Raised when no rendezvous circuits are identified for an onion service
    we have already successfully loaded."""

# These exceptions are known to crash the Crawler
_sketchy_exceptions = [http.client.RemoteDisconnected]


class Crawler:
    """Crawls your onions, but also manages Tor, drives Tor Browser, and uses
    information from your Tor cell log and stem to collect cell sequences."""
    def __init__(self, 
                 take_ownership=True, # Tor dies when the Crawler does
                 torrc_config={"CookieAuth": "1"},
                 tor_log="/var/log/tor/tor.log",
                 tor_cell_log="/var/log/tor/tor_cell_seq.log",
                 control_port=9051,
                 socks_port=9050, 
                 run_in_xvfb=True,
                 tbb_path=join("/opt","tbb","tor-browser_en-US"),
                 tb_log_path=join(_log_dir,"firefox.log"),
                 tb_tor_cfg=USE_RUNNING_TOR,
                 page_load_timeout=20,
                 wait_on_page=5,
                 wait_after_closing_circuits=0,
                 restart_on_sketchy_exception=True,
                 additional_control_fields={},
                 db_handler=None):

        self.logger = setup_logging(_log_dir, "crawler")

        self.torrc_config = torrc_config
        self.socks_port = find_free_port(socks_port, control_port)
        self.torrc_config.update({"SocksPort": str(self.socks_port)})
        self.control_port = find_free_port(control_port, self.socks_port)
        self.torrc_config.update({"ControlPort": str(self.control_port)})
        self.torrc_config.update({"Log": "INFO file {}".format(tor_log)})
        self.logger.info("Starting tor process with config "
                         "{torrc_config}.".format(**locals()))
        self.tor_process = launch_tor_with_config(config=self.torrc_config,
                                                  take_ownership=take_ownership)
        self.authenticate_to_tor_controlport()

        self.logger.info("Opening cell log stream...")
        self.cell_log = open(tor_cell_log, "rb")

        if run_in_xvfb:
            self.logger.info("Starting Xvfb...")
            self.run_in_xvfb = True
            self.virtual_framebuffer = start_xvfb()

        self.logger.info("Starting Tor Browser...")
        self.tb_driver = TorBrowserDriver(tbb_path=tbb_path,
                                          tor_cfg=tb_tor_cfg,
                                          tbb_logfile_path=tb_log_path,
                                          socks_port=self.socks_port,
                                          control_port=self.control_port)

        self.wait_after_closing_circuits = wait_after_closing_circuits
        self.page_load_timeout = page_load_timeout
        self.tb_driver.set_page_load_timeout(page_load_timeout)
        self.wait_on_page = wait_on_page
        self.restart_on_sketchy_exception = restart_on_sketchy_exception

        self.control_data = self.get_control_data(page_load_timeout,
                                                  wait_on_page,
                                                  wait_after_closing_circuits,
                                                  additional_control_fields)
        self.db_handler = db_handler
        if db_handler:
            self.crawlid = self.db_handler.add_crawl(self.control_data)


    def authenticate_to_tor_controlport(self):
        self.logger.info("Authenticating to the tor controlport...")
        try:
            self.controller = Controller.from_port(port=self.control_port)
        except stem.SocketError as exc:
            panic("Unable to connect to tor on port {self.control_port}: "
                  "{exc}".format(**locals()))
        try:
            self.controller.authenticate()
        except stem.connection.MissingPassword:
            panic("Unable to authenticate to tor controlport. Please add "
                  "`CookieAuth 1` to your tor configuration file.")


    def get_control_data(self, page_load_timeout, wait_on_page,
                         wait_after_closing_circuits,
                         additional_control_fields):
        """Gather metadata about the crawler instance."""
        control_data = {}
        # Configuration settings
        control_data["page_load_timeout"] = page_load_timeout
        control_data["wait_on_page"] = wait_on_page
        control_data["wait_after_closing_circuits"] = \
                wait_after_closing_circuits
        if additional_control_fields:
            control_data.update(additional_control_fields)
        # System facts
        control_data["kernel"] = platform.system()
        control_data["kernel_version"] = platform.release()
        control_data["os"] = platform.version()
        control_data["python_version"] = platform.python_version()
        ip = urlopen("https://api.ipify.org").read().decode()
        control_data["ip"] = ip
        # This API seems to be unstable and we haven't found a suitable
        # alternative :(
        try:
            asn_geoip = urlopen("http://api.moocher.io/ip/{}".format(ip))
            asn_geoip = literal_eval(asn_geoip.read().decode())
            control_data["asn"] = asn_geoip.get("ip").get("as").get("asn")
            control_data["city"] = asn_geoip.get("ip").get("city")
            control_data["country"] = asn_geoip.get("ip").get("country")
        except urllib.error.HTTPError:
            self.logger.warning("Unable to query ASN API and thus some "
                                "control data may be missing from this run.")
        control_data["tor_version"] = self.controller.get_version().version_str
        control_data["tb_version"] = self.tb_driver.tb_version
        # Tor will have multiple entry nodes in its state file, but will
        # choose the first sequential one that is up as its entry guard.
        entry_nodes = self.controller.get_info("entry-guards").split('\n')
        control_data["entry_node"] = next(re.search("[0-9A-F]{40}", g).group(0)
                                          for g in entry_nodes
                                          if re.search("up", g))
        control_data["crawler_version"] = _version
        return control_data


    def __enter__(self):
        return self


    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return self


    def __del__(self):
        self.close()


    def close(self):
        self.logger.info("Beginning Crawler exit process...")
        if "tb_driver" in dir(self):
            self.logger.info("Closing Tor Browser...")
            self.tb_driver.quit()
        if "virtual_framebuffer" in dir(self):
            self.logger.info("Closing the virtual framebuffer...")
	    # A bug in pyvirtualdisplay triggers a KeyError exception when closing a
            # virtual framebuffer if the $DISPLAY environment variable is not set.
            try:
                stop_xvfb(self.virtual_framebuffer)
            except KeyError:
                pass
        if "cell_log" in dir(self):
            self.logger.info("Closing the Tor cell stream...")
            self.cell_log.close()
        if "tor_process" in dir(self):
            self.logger.info("Killing the tor process...")
            self.tor_process.kill()
        self.logger.info("Crawler exit completed.")


    def collect_onion_trace(self, url, hsid=None, extra_fn=None, trace_dir=None,
                            iteration=0):
        """Crawl an onion service and collect a complete cell sequence for the
        activity at the time. Also, record additional information about the
        circuits with stem. Optionally, pass a function to execute additional
        actions after the page has loaded."""
        # Todo: create collect_trace method that works for regular sites as
        # well
        assert ".onion" in url, ("This method is only suitable for crawling "
                                 "onion services.")

        self.logger.info("{url}: closing existing circuits before starting "
                         "crawl.".format(**locals()))
        for circuit in self.controller.get_circuits():
            self.controller.close_circuit(circuit.id)

        sleep(self.wait_after_closing_circuits)

        if not trace_dir:
            trace_dir = self.make_ts_dir()
        trace_name = urllib.parse.quote(url, safe="") + "-" + str(iteration)
        trace_path = join(trace_dir, trace_name)

        start_idx = self.get_cell_log_pos()

        try:
            self.crawl_url(url)
            rend_circ_ids = self.get_rend_circ_ids(url)
            if extra_fn:
                self.execute_extra_fn(url, trace_path, start_idx)
        except CrawlerLoggedError:
            return "failed"
        except CrawlerNoRendCircError:
            self.save_debug_log(url, trace_path, start_idx)
            return "failed"
        except:
            self.logger.exception("{url}: unusual exception "
                                  "encountered:".format(**locals()))
            # Also log active circuit info
            self.controller.get_circuits()

            exc_type, exc_value, exc_traceback = exc_info()
            if exc_type in _sketchy_exceptions:
                self.save_debug_log(url, trace_path, start_idx)
                if self.restart_on_sketchy_exception:
                    self.restart_tb()

            return "failed"

        self.logger.info("{url}: saving full trace...".format(**locals()))
        end_idx = self.get_cell_log_pos()
        full_trace = self.get_full_trace(start_idx, end_idx)

        # Save the trace to the database or write to file
        if self.db_handler:
            try:
                new_example = {'hsid': hsid,
                               'crawlid': self.crawlid,
                               't_scrape': get_timestamp("db")}
            except NameError:
                panic("If using the database, and calling collect_onion_trace "
                      "directly, you must specify the hsid of the site.")
            exampleid = self.db_handler.add_example(new_example)
            self.db_handler.add_trace(str(full_trace), exampleid)
        else:
            with open(trace_path+"-full", "wb") as fh:
                fh.write(full_trace)

        return "succeeded"


    def make_ts_dir(self, parent_dir=_log_dir, raw_dir_name="batch"):
        """Creates a timestamped folder to hold a group of traces."""
        raw_dirpath = join(parent_dir, raw_dir_name)
        ts = get_timestamp("log")
        ts_dir = timestamp_file(raw_dirpath, ts, is_dir=True)
        symlink_cur_to_latest(raw_dirpath, ts)

        with open(join(ts_dir, "control.pickle"), "wb") as fh:
            pickle.dump(self.control_data, fh)

        return ts_dir


    def get_cell_log_pos(self):
        """Returns the current position of the last byte in the Tor cell log."""
        return self.cell_log.seek(0, SEEK_END)


    def crawl_url(self, url):
        """Load a web page in Tor Browser and optionally pass a function
        to execute custom actions on it."""

        self.logger.info("{url}: starting page load...".format(**locals()))

        try:
            self.tb_driver.load_url(url, wait_on_page=self.wait_on_page,
                                    wait_for_page_body=True)
        except TimeoutException:
            self.logger.warning("{url}: timed out.".format(**locals()))
            raise CrawlerLoggedError
        except http.client.CannotSendRequest:
            self.logger.warning("{url}: cannot send request--improper "
                                "connection state.".format(**locals()))
            raise CrawlerLoggedError

        # Make sure we haven't just hit an error page or nothing loaded
        try:
            if (self.tb_driver.is_connection_error_page
                or self.tb_driver.current_url == "about:newtab"):
                raise CrawlerReachedErrorPage
        except CrawlerReachedErrorPage:
            self.logger.warning("{url}: reached connection error "
                                "page.".format(**locals()))
            raise CrawlerLoggedError

        self.logger.info("{url}: successfully loaded.".format(**locals()))


    def get_rend_circ_ids(self, url):
        """Returns the rendezvous circuit id(s) associated with a given onion
        service."""
        self.logger.info("{url}: collecting circuit "
                         "information...".format(**locals()))
        active_circs = self.controller.get_circuits()
        rend_circ_ids = set()

        for circ in active_circs:
            if (circ.purpose == "HS_CLIENT_REND" and
                circ.socks_username and 
                circ.socks_username in url):
                rend_circ_ids.add(circ.id)

        # If everything goes perfect, we should only see one. Multiple indicate
        # the first failed. Zero indicates one closed abruptly (or there's an
        # error with stem--still waiting on data to confirm or deny).
        rend_circ_ct = len(rend_circ_ids)
        self.logger.info("{url}: {rend_circ_ct} associated rendezvous circuits "
                         "discovered.".format(**locals()))
        if rend_circ_ct == 0:
            raise CrawlerNoRendCircError

        return rend_circ_ids


    def execute_extra_fn(self, url, trace_path, start_idx):
        self.logger.info("{url}: executing extra function "
                         "code...".format(**locals()))
        extra_fn(self, url, trace_path, start_idx)
        self.logger.info("{url}: extra function executed "
                         "successfully.".format(**locals()))


    def save_debug_log(self, url, trace_path, start_idx):
        self.logger.warning("{url}: saving debug log...".format(**locals()))
        exc_time = self.get_cell_log_pos()
        trace = self.get_full_trace(start_idx, exc_time)
        with open(trace_path + "@debug", "wb") as fh:
            fh.write(trace)



    def get_full_trace(self, start_idx, end_idx):
        """Returns the Tor DATA cells transmitted over a circuit during a
        specified time period."""
        # Sanity check
        assert start_idx >= 0 and end_idx > 0, ("Invalid (negative) logfile "
                                                "position")
        assert end_idx > start_idx, ("logfile section end_idx must come "
                                     "after start_idx")

        self.cell_log.seek(start_idx, SEEK_SET)
        return self.cell_log.read(end_idx - start_idx)


    def restart_tb(self):
        """Restarts the Tor Browser."""
        self.logger.info("Restarting the Tor Browser...")
        self.tb_driver.quit()
        self.tb_driver = TorBrowserDriver(tbb_path=tbb_path,
                                          tor_cfg=USE_RUNNING_TOR,
                                          tbb_logfile_path=tb_log_path,
                                          socks_port=self.socks_port,
                                          control_port=self.control_port)
        self.logger.info("Tor Browser restarted...")


    def collect_set_of_traces(self, url_set, extra_fn=None, trace_dir=None,
                              iteration=0, shuffle=True, retry=True,
                              url_to_id_mapping=None):
        """Collect a set of traces."""
        if self.db_handler:
            if not url_to_id_mapping:
                url_to_id_mapping = url_set
            trace_dir = None
        elif not trace_dir:
                trace_dir = self.make_ts_dir()

        set_size = len(url_set)
        self.logger.info("Saving set of {set_size} traces to "
                         "{trace_dir}.".format(**locals()))

        # Converts both sets (from pickle files) and dicts (whose keys are
        # URLs--from database) to URL lists
        url_set = list(url_set)
        if shuffle:
            random.shuffle(url_set)

        failed_urls = []

        for url_idx in range(set_size):
            self.logger.info("Collecting trace {} of "
                             "{set_size}...".format(url_idx+1, **locals()))
            url = url_set[url_idx]
            if self.db_handler:
                hsid = url_to_id_mapping[url]
            else:
                hsid = None

            if (self.collect_onion_trace(url, hsid=hsid, extra_fn=extra_fn,
                                         trace_dir=trace_dir,
                                         iteration=iteration) == "failed"
                and retry):
                failed_urls.append(url)

        if failed_urls:
            failed_ct = len(failed_urls)
            self.logger.info("Retrying {failed_ct} of {set_size} traces that "
                             "failed.".format(**locals()))
            self.collect_set_of_traces(failed_urls, extra_fn=extra_fn,
                                       trace_dir=trace_dir,
                                       iteration=iteration, shuffle=shuffle,
                                       retry=False)


    def crawl_monitored_nonmonitored(self, monitored_class, nonmonitored_class,
                                     extra_fn=None, shuffle=True, retry=True,
                                     monitored_name="monitored",
                                     nonmonitored_name="nonmonitored",
                                     url_to_id_mapping=None, ratio=40):
        """Crawl a monitored class ratio times interspersed between the
        crawling of a(n ostensibly larger) non-monitored class."""
        if self.db_handler:
            if not url_to_id_mapping:
                url_to_id_mapping = nonmonitored_class
                url_to_id_mapping.update(monitored_class)
            trace_dir, mon_trace_dir, nonmon_trace_dir = (None,) * 3
        else:
            trace_dir = self.make_ts_dir()
            mon_trace_dir = join(trace_dir, monitored_name)
            mkdir(mon_trace_dir)
            nonmon_trace_dir = join(trace_dir, nonmonitored_name)
            mkdir(nonmon_trace_dir)

        # db: calling list on a dict returns a list of its keys (URLs)
        # pickle: calling list on set is necessary to make it shuffleable
        nonmonitored_class = list(nonmonitored_class)
        monitored_class = list(monitored_class)

        nonmonitored_class_ct = len(nonmonitored_class)
        chunk_size = int(nonmonitored_class_ct / ratio)

        if shuffle:
            random.shuffle(nonmonitored_class)
            random.shuffle(monitored_class)

        for iteration in range(ratio):

            self.logger.info("Beginning iteration {i} of {ratio} in the "
                             "{monitored_name} class".format(i=iteration+1,
                                                             **locals()))
            self.collect_set_of_traces(monitored_class,
                                       trace_dir=mon_trace_dir,
                                       iteration=iteration,
                                       url_to_id_mapping=url_to_id_mapping)

            slice_lb = iteration * chunk_size
            slice_ub = min((iteration + 1) * chunk_size, nonmonitored_class_ct)
            self.logger.info("Crawling services {} through {slice_ub} of "
                             "{nonmonitored_class_ct} in the "
                             "{nonmonitored_name} "
                             "class".format(slice_lb + 1, **locals()))
            self.collect_set_of_traces(nonmonitored_class[slice_lb:slice_ub],
                                       trace_dir=nonmon_trace_dir)

def _securedrop_crawl():
    config = get_config()
    config = config["crawler"]
    if config.getboolean("use_database"):
        fpdb = RawStorage()
        class_data = fpdb.get_onions(config["hs_history_lookback"])
    else:
        fpdb = None 
        with open(join(_log_dir, config["class_data"]), 'rb') as pj:
            class_data = pickle.load(pj)

    nonmonitored_name, monitored_name = class_data.keys()
    nonmonitored_class, monitored_class = class_data.values()

    with Crawler(page_load_timeout=config.getint("page_load_timeout"),
                 wait_on_page=config.getint("wait_on_page"),
                 wait_after_closing_circuits=config.getint("wait_after_closing_circuits"),
                 restart_on_sketchy_exception=config.getboolean("restart_on_sketchy_exception"),
                 db_handler=fpdb,
                 torrc_config={"CookieAuthentication": "1",
                               "EntryNodes": config["entry_nodes"]}) as crawler:
        crawler.crawl_monitored_nonmonitored(monitored_class,
                                             nonmonitored_class,
                                             monitored_name=monitored_name,
                                             nonmonitored_name=nonmonitored_name,
                                             ratio=config.getint("monitored_nonmonitored_ratio"))

if __name__ == "__main__":
    _securedrop_crawl()
