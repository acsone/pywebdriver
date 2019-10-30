import json
import logging

import easyctep
from flask import request, jsonify

from pywebdriver import app, config, drivers
from .payment_base_driver import PaymentTerminalDriver

_logger = logging.getLogger(__name__)

CONFIG_SECTION = "easyctep_driver"


def get_incident_codes():
    return {"1803": _("Timeout"), "2629": _("Cancelled")}


class PywdTerminalManager(easyctep.TerminalManager):
    _driver = None

    def on_terminal_connect(self, terminal):
        super().on_terminal_connect(terminal)
        self._driver._set_terminal_status(terminal.terminal_id, "connected")
        terminal.last_transaction_status(self._driver)

    def on_terminal_diconnect(self, terminal):
        self._driver._set_terminal_status(terminal.terminal_id, "disconnected")
        super().on_terminal_disconnect(terminal)


class EasyCtepDriver(PaymentTerminalDriver, easyctep.SaleResultListener):
    def __init__(self):
        super(EasyCtepDriver, self).__init__()
        self.print_receipt = config.getboolean(
            CONFIG_SECTION, "print_receipt", fallback=False
        )
        port = config.getint(CONFIG_SECTION, "service_port", fallback=9000)
        certification_logfile = config.get(
            CONFIG_SECTION, "certification_logfile", fallback=None
        )
        _logger.info("creating easyctep terminal manager on port %s", port)
        self._terminal_manager = PywdTerminalManager.create_tcp_ip(port)
        self._terminal_manager._driver = self
        if certification_logfile:
            self._terminal_manager.set_certification_mode(certification_logfile)
        self._terminal_manager.start()

    # easyctep.SaleResultListener interface

    def on_sale_result_success(self, terminal_id, **kwargs):
        _logger.info("on_sale_result_success on terminal %s: %s", terminal_id, kwargs)
        transaction_id = kwargs.get("sale_system_action_identifier")
        if transaction_id:
            self.end_transaction(terminal_id, transaction_id, success=True)
        else:
            _logger.error(
                "received on_sale_result_success without sale_system_action_identifier"
            )
        client_ticket = kwargs.get("client_ticket")
        if client_ticket and self.print_receipt:
            terminal = self._terminal_manager.terminal_by_id(terminal_id)
            terminal.send_print_ticket_transaction(client_ticket)

    def on_sale_result_error(self, terminal_id, **kwargs):
        _logger.warning("on_sale_result_error on terminal %s: %s", terminal_id, kwargs)
        transaction_id = kwargs.get("sale_system_action_identifier")
        description = kwargs.get("description")
        incident_code = kwargs.get("incident_code")
        self.end_transaction(
            terminal_id,
            transaction_id,
            success=False,
            status=get_incident_codes().get(incident_code, _("Error")),
            status_details=description,
        )

    # / from easyctep.SaleResultListener

    def _make_transaction_uuid(self):
        return self._terminal_manager.new_sale_system_action_identifier()

    def transaction_start(self, data):
        payment_info = data["payment_info"]
        transaction_id = data["transaction_id"]
        terminal_id = payment_info.get("terminal_id", "0")
        app.logger.info(
            "transaction start for terminal %s: %s", terminal_id, payment_info
        )
        try:
            amount = payment_info.get("amount")
            if not amount or float(amount) < 0:
                raise ValueError("Invalid amount {}".format(amount))
            merchant_reference = str(payment_info.get("order_id", ""))
            terminal = self._terminal_manager.terminal_by_id(terminal_id)
            terminal.send_sale_transaction(
                amount, merchant_reference, transaction_id, self
            )
        except Exception as e:
            app.logger.error("error sending transaction to terminal", exc_info=True)
            message = str(e)
            self.end_transaction(terminal_id, transaction_id, False, status=message)


@app.route("/hw_proxy/payment_terminal_transaction_start", methods=["POST"])
def payment_terminal_transaction_start():
    # TODO why json in json?
    payment_info = json.loads(request.json["params"]["payment_info"])
    terminal_id = payment_info.get("terminal_id", "0")
    transaction = easyctep_driver.begin_transaction(terminal_id)
    easyctep_driver.push_task(
        "transaction_start",
        data=dict(
            payment_info=payment_info, transaction_id=transaction["transaction_id"]
        ),
    )
    return jsonify(jsonrpc="2.0", result=transaction)


easyctep_driver = EasyCtepDriver()
drivers["easyctep_driver"] = easyctep_driver
