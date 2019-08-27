# -*- coding: utf-8 -*-
###############################################################################
#
#   Copyright (C) 2019 ACSONE SA/NV (https://www.acsone.eu/).
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU Affero General Public License as
#   published by the Free Software Foundation, either version 3 of the
#   License, or (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU Affero General Public License for more details.
#
#   You should have received a copy of the GNU Affero General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
from collections import OrderedDict
from configparser import NoOptionError
import logging
import time
import os, sys

import simplejson as json
from flask_cors import cross_origin
from flask import request, jsonify, render_template

from pywebdriver import app, config, drivers

from .base_driver import ThreadDriver

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from easyctep import (
    SaleResultListener,
    TerminalManager,
    TerminalLookupError,
    Terminal,
    get_library_version
)

logger = logging.getLogger(__name__)

class LimitedDict(OrderedDict):
    """ A dictionary that only keeps the last few added keys
        This serves as a FIFO cache """
    def __init__(self, size=20):
        super(LimitedDict, self).__init__()
        self._max_size = size

    def __setitem__(self, key, value):
        if len(self) == self._max_size:
            self.popitem(last=False)
        super(LimitedDict, self).__setitem__(key, value)


class CTEPSaleResultListener(SaleResultListener):

    def __init__(self, *a, **kw):
        super(CTEPSaleResultListener, self).__init__(*a, **kw)
        self.result = None

    def on_sale_result_success(self, terminal_id, **kwargs):
        self.result = True
        self._callback(self.result, **kwargs)
        logger.warning(
            "on_sale_result_success on terminal %s: %s", terminal_id, kwargs
        )

    def on_sale_result_error(self, terminal_id, **kwargs):
        self.result = False
        self._callback(self.result,** kwargs)
        logger.warning("on_sale_result_error on terminal %s: %s", terminal_id, kwargs)

    def wait_for_result(self, callback):
        self._callback = callback
        while self.result is None:
            time.sleep(1)


ctep_sale_result_listener = CTEPSaleResultListener()


class CTEPDriver(ThreadDriver):

    def __init__(self, cfg, *a, **kw):
        super(CTEPDriver, self).__init__(*a, **kw)
        self._init_driver(config=cfg)
        
    def _init_driver(self, config):
        """
        Initialize CTEP driver
        """
        logger.info("%s - %s " % (self, "CTEP Initialization"))
        logger.info("%s - %s " % (self, " - ".join(["CTEP Library version", get_library_version()])))
        # Specific to match 'sale identifier' with Sale Order number
        # Each entry is composed by <salesystemIdentifier> : {'order_id': <order reference>, 'amount': <amount>}
        self.current_transactions = {}
        # Other properties
        self.cfg = config
        self.service_port = self.cfg.get('service_port', 9000)
        self.terminal = False
        self.in_transaction = False
        self.transactions_count = 0
        self.transactions_cache = LimitedDict()
        self.service = TerminalManager.make_tcp_ip_service(self.service_port)

    def run(self):
        """
        Launched when Application start
        """
        self.service.start()
        # Wait asynchronously for terminal
        self.push_task("_wait_for_terminal")
        super(CTEPDriver, self).run()

    def _wait_for_terminal(self, data):
        while True:
            try:
                self.terminal = self.service.current_terminal()
                return
            except TerminalLookupError:
                time.sleep(1)

    def get_status(self):
        """
        Return Terminal status
        """
        status = {
            'status': 'connected' if self.terminal else 'disconnected',
            'in_transaction': self.in_transaction,
            'transactions_count': self.transactions_count,
            'latest_transactions': self.transactions_cache,
        }
        return status

    def get_vendor_product(self):
        return 'easyctep'
 
    def get_payment_info_from_price(self, price, payment_mode):
        logger.info("Payment mode: %s", payment_mode)
        return {
            'amount': price,
            'payment_mode': payment_mode,
            'currency_iso': 'EUR',
        }

    def _wait_for_sale_result(self, data):
        """
        Wait for sale transaction result
        """
        ctep_sale_result_listener.wait_for_result(self.set_result)

    def transaction_start(self, info):
        self.in_transaction = True
        self.transactions_count += 1
        info = json.loads(info)
        order_id = info['order_id']
        if not self.terminal:
            logger.warn("Terminal not registered yet, can't start transaction")
            return
        logger.info("Starting transaction")
        amount = info['amount']
        sale_identifier = self.service.new_sale_system_action_identifier()
        # TODO: Remove
        sale_identifier = int(sale_identifier)

        self.current_transactions[sale_identifier] = {
            'order_id': order_id,
            'amount': amount,
        }
        self.terminal.send_sale_transaction(
            amount, order_id, sale_identifier, ctep_sale_result_listener
        )
        self.push_task("_wait_for_sale_result")
    
    def set_result(self, result, **kwargs):
        """
        Callback method to be called by SaleListener
        """
        self.in_transaction = False
        if not result:
            return
        sale_identifier = kwargs.get("sale_system_action_identifier")
        if sale_identifier:
            sale = self.current_transactions[sale_identifier]
            self.transactions_cache.setdefault(sale.get("order_id"), []).append({
                        'amount_cents': kwargs.get("authorized_amount"),
                    })

def load_driver_config():
    driver_config = {}
    # Put here mandatory config entiries
    for key in ():
        try:
            driver_config[key] = config.get('easyctep_driver', key)
        except NoOptionError:
            raise Exception("Missing configuration for ctep driver: %s" % key)
    for key, default in (('print_receipt', 'yes'),):
        if config.has_option('easyctep_driver', key):
            driver_config[key] = config.get('easyctep_driver', key)
        else:
            driver_config[key] = default
    return driver_config


easyctep_driver = CTEPDriver(load_driver_config())
drivers['easyctep'] = easyctep_driver
easyctep_driver.start()


@app.route(
    '/hw_proxy/payment_terminal_transaction_start',
    methods=['POST', 'GET', 'PUT', 'OPTIONS'])
@cross_origin(headers=['Content-Type'])
def payment_terminal_transaction_start():
    app.logger.debug('CTEP: Call payment_terminal_transaction_start')
    payment_info = request.json['params']['payment_info']
    app.logger.debug('CTEP: payment_info=%s', payment_info)
    easyctep_driver.push_task('transaction_start', payment_info)
    return jsonify(jsonrpc='2.0', result=True)


@app.route('/ctep_status.html', methods=['POST'])
@cross_origin()
def ctep_status():
    info = easyctep_driver.get_payment_info_from_price(
        float(request.values['price']),
        request.values['payment_mode'])
    info['order_id'] = "TEST"
    app.logger.debug('CTEP status info=%s', info)
    easyctep_driver.push_task('transaction_start', json.dumps(
        info, sort_keys=True))
    return render_template('ctep_status.html')
