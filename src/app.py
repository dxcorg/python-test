from flask import Flask, jsonify
import socket

app = Flask(__name__)

@app.route('/api/v1/details', methods=['GET'])
def get_details():
    # Sample data to return
    details = {
        'name': 'Sample API',
        'version': '1.0',
        'description': 'This is a sample API endpoint.',
        'hostname': socket.gethostname(),
    }
    return jsonify(details)

@app.route('/api/v1/health', methods=['GET'])
def get_health():
    health_status = {
        'status': 'healthy'
    }
    return jsonify(health_status)

app.run(host='0.0.0.0', port=5000, debug=True   )

#'api/v1/details'
#'api/v1/health'



