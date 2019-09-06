#!/usr/bin/env python
import os

from pywebdriver import app, config, drivers

def main():
    host = config.get('flask', 'host')
    port = config.getint('flask', 'port')
    debug = config.getboolean('flask', 'debug') 
    if config.getboolean('application', 'print_status_start'):
        if 'escpos' in drivers:
            drivers['escpos'].push_task('printstatus')
    flask_args = dict(
        host=host,
        port=port,
        debug=debug,
        processes=0,
        threaded=True
    )
    if config.has_option('flask', 'sslcert'):
        sslcert = config.get('flask', 'sslcert')
        if sslcert:
            if not config.has_option('flask', 'sslkey'):
                print("If you want SSL, you must also provide the sslkey")
                sys.exit(-1)
            sslkey = config.get('flask', 'sslkey')
            if not os.path.exists(sslcert):
                print("SSL cert not found at", sslcert)
                sys.exit(-1)
            if not os.path.exists(sslkey):
                print("SSL key not found at", sslkey)
                sys.exit(-1)
            flask_args['ssl_context'] = (sslcert, sslkey)
    app.run(**flask_args)

# Run application
if __name__ == '__main__':
    main()
