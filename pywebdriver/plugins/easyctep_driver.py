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
from configparser import NoOptionError
import logging
import time
import os
import sys

import simplejson as json
from flask_cors import cross_origin
from flask import request, jsonify, render_template

from pywebdriver import app, config, drivers

from .payment_base_driver import PaymentTerminalDriver
from easyctep import (
    SaleResultListener,
    TerminalManager,
    Terminal,
    get_library_version
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)6s %(thread)5s %(message)s"
)

logger = logging.getLogger(__name__)


class CTEPSaleResultListener(SaleResultListener):

    def __init__(self, *a, **kw):
        super(CTEPSaleResultListener, self).__init__(*a, **kw)
        self.result = None

    def on_sale_result_success(self, terminal_id, **kwargs):
        """
        Catch sale result success and pass it to driver callback
        :param terminal_id:
        :param kwargs:
        :return:
        """
        self.result = True
        self._callback(terminal_id, self.result, **kwargs)
        logger.info(
            "Sale result succeeded on terminal %s: %s", terminal_id, kwargs
        )

    def on_sale_result_error(self, terminal_id, **kwargs):
        """
        Catch sale result error and pass it to driver callback
        :param terminal_id:
        :param kwargs:
        :return:
        """
        self.result = False
        self._callback(terminal_id, self.result, ** kwargs)
        logger.warning(
            "Sale result error on terminal %s: %s", terminal_id, kwargs
        )

    def wait_for_result(self, callback):
        """
        Will associate the callback function to sale result listener
        :param callback:
        :return:
        """
        self._callback = callback
        while self.result is None:
            time.sleep(1)


class CTEPTerminalManager(TerminalManager):

    def on_terminal_connect(self, terminal):
        """
        Will catch terminal connections and send result to driver
        :param terminal: Terminal()
        :return:
        """
        res = super(CTEPTerminalManager, self).on_terminal_connect(terminal)
        self._callback(terminal.terminal_id, connected=True)
        return res

    def on_terminal_disconnect(self, terminal_id):
        """
                Will catch terminal disconnections and send result to driver
                :param terminal_id: byte
                :return:
                """
        res = super(CTEPTerminalManager, self).on_terminal_disconnect(
            terminal_id)
        self._callback(terminal_id, connected=False)
        return res

    def register_ctep_callback(self, callback):
        """
        Register Ctep driver callback function
        :param callback:
        :return:
        """
        self._callback = callback


ctep_sale_result_listener = CTEPSaleResultListener()


class CTEPDriver(PaymentTerminalDriver):

    def __init__(self, cfg, *a, **kw):
        super(CTEPDriver, self).__init__(cfg, *a, **kw)
        self._init_driver()
        
    def _init_driver(self):
        """
        Initialize CTEP driver
        """
        logger.info("%s - %s " % (self, "CTEP Initialization"))
        logger.info(
            "%s - %s " %
            (self, " - ".join(["CTEP Library version", get_library_version()]))
        )
        # Specific to match 'sale identifier' with Sale Order number
        # Each entry is composed by <salesystemIdentifier> :
        # {'order_id': <order reference>, 'amount': <amount>}
        self.current_transactions = {}
        ctep_sale_result_listener._callback = self.set_result

        service_port = int(self.config.get('service_port', 9000))
        self.print_receipt = True\
            if self.config.get('print_receipt', False) == 'True' else False
        logger.info("starting CTEP service on port %s", service_port)
        self.service = CTEPTerminalManager.create_tcp_ip(service_port)
        if self.config.get('certification_mode', False):
            self.service.set_certification_mode(
                os.path.join(
                    os.path.dirname(__file__), "easyctep_service.log"))

        # run TerminalManager service
        self.service.start()
        # Wait asynchronously for terminal
        self.push_task("_wait_for_terminal")

    def _set_terminal_status(self, terminal_id, connected):
        """
        This is intended to be passed as callback to TerminalManager
        It is used to update the terminal state in the driver
        :param terminal:
        :param connected:
        :return:
        """
        state = 'connected' if connected else 'disconnected'
        if state == 'connected':
            terminal = self.service.terminal_by_id(terminal_id)
            # Get last transaction status
            # It will go into the callback to set transaction status
            # if this was raised after a terminal disconnection
            terminal.last_transaction_status(ctep_sale_result_listener)
        self._set_state(terminal_id, state)
        logger.info(
            'CTEP : Terminal %s changed status to %s' % (terminal_id, state))

    def _wait_for_terminal(self, data):
        CTEPTerminalManager.register_ctep_callback(
            CTEPTerminalManager, self._set_terminal_status)

    def get_vendor_product(self):
        return 'easyctep'
 
    def get_payment_info_from_price(self, price, payment_mode):
        """
        This is used for the test interface
        :param price:
        :param payment_mode:
        :return:
        """
        return {
            'amount': price,
            'payment_mode': payment_mode,
            'currency_iso': 'EUR',
        }

    def _wait_for_sale_result(self, data):
        """
        Wait for sale transaction result and pass the callback
        """
        ctep_sale_result_listener.wait_for_result(self.set_result)

    def _transaction_start(self, terminal_id, payment_info):
        """
        Start a CTEP transaction:
            * Build a new sale unique identifier
            * Send the transaction to the registered terminal
            * Wait for result (use of CTEPSaleResultListener for callbacks)
        :param terminal_id: str
        :param payment_info: dict
        :return:
        """
        if terminal_id not in self.terminals or\
                self.terminals[terminal_id]['state'] != 'connected':
            logger.warning(
                "CTEP : Terminal not registered yet, can't start transaction")
            return
        order_id = payment_info['order_id']
        amount = payment_info['amount']
        sale_identifier = self.service.new_sale_system_action_identifier()
        # TODO: Remove
        sale_identifier = int(sale_identifier)
        # Add transaction to current ones (the cache)
        self.current_transactions[sale_identifier] = {
            'order_id': order_id,
            'amount': amount,
        }
        logger.info("CTEP : Starting transaction")
        self.service._terminals[terminal_id].send_sale_transaction(
            amount, order_id, sale_identifier, ctep_sale_result_listener
        )
        self.push_task("_wait_for_sale_result")
    
    def set_result(self, terminal_id, result, **kwargs):
        """
        Callback method to be called by SaleListener
        Get the transaction concerned by the trigger and if result == True,
        add a line to transactions_cache.
        """
        self.terminals[terminal_id]['in_transaction'] = False
        if not result:
            return
        sale_identifier = kwargs.get("sale_system_action_identifier")
        if sale_identifier and sale_identifier in self.current_transactions:
            sale = self.current_transactions[sale_identifier]
            order_id = sale.get("order_id")
            authorized_amount = kwargs.get("authorized_amount")
            self._add_transaction(order_id, authorized_amount, terminal_id)
            ticket = kwargs.get('client_ticket')
            if self.print_receipt and ticket:
                terminal = self.service.terminal_by_id(terminal_id)
                terminal.send_print_ticket_transaction(ticket)


def load_driver_config():
    """
    Utility function to load correctly configuration options
    :return:
    """
    driver_config = {}
    default_keys = (
        ('print_receipt', 'yes'),
        ('service_port', 9000),
        ('certification_mode', False))
    # Put here mandatory config entries
    for key in ():
        try:
            driver_config[key] = config.get('easyctep_driver', key)
        except NoOptionError:
            raise Exception("Missing configuration for ctep driver: %s" % key)
    for key, default in default_keys:
        if config.has_option('easyctep_driver', key):
            driver_config[key] = config.get('easyctep_driver', key)
        else:
            driver_config[key] = default
    return driver_config


easyctep_driver = CTEPDriver(load_driver_config())
drivers['easyctep'] = easyctep_driver
if not easyctep_driver.is_alive():
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
