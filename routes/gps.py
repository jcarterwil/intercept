"""GPS routes for gpsd daemon support."""

from __future__ import annotations

import queue
import time
from typing import Generator

from flask import Blueprint, jsonify, request, Response

from utils.logging import get_logger
from utils.sse import format_sse
from utils.gps import (
    get_gps_reader,
    start_gpsd,
    stop_gps,
    get_current_position,
    get_manual_position,
    set_manual_position,
    clear_manual_position,
    GPSPosition,
)

logger = get_logger('intercept.gps')

gps_bp = Blueprint('gps', __name__, url_prefix='/gps')

# Queue for SSE position updates
_gps_queue: queue.Queue = queue.Queue(maxsize=100)


def _position_callback(position: GPSPosition) -> None:
    """Callback to queue position updates for SSE stream."""
    try:
        _gps_queue.put_nowait(position.to_dict())
    except queue.Full:
        # Discard oldest if queue is full
        try:
            _gps_queue.get_nowait()
            _gps_queue.put_nowait(position.to_dict())
        except queue.Empty:
            pass


@gps_bp.route('/auto-connect', methods=['POST'])
def auto_connect_gps():
    """
    Automatically connect to gpsd if available.

    Called on page load to seamlessly enable GPS if gpsd is running.
    Returns current status if already connected.
    """
    import socket

    # Check if already running
    reader = get_gps_reader()
    if reader and reader.is_running:
        position = reader.position
        return jsonify({
            'status': 'connected',
            'source': 'gpsd',
            'has_fix': position is not None,
            'position': position.to_dict() if position else None
        })

    # Try to connect to gpsd on localhost:2947
    host = 'localhost'
    port = 2947

    # First check if gpsd is reachable
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        sock.connect((host, port))
        sock.close()
    except Exception:
        return jsonify({
            'status': 'unavailable',
            'message': 'gpsd not running'
        })

    # Clear the queue
    while not _gps_queue.empty():
        try:
            _gps_queue.get_nowait()
        except queue.Empty:
            break

    # Start the gpsd client
    success = start_gpsd(host, port, callback=_position_callback)

    if success:
        return jsonify({
            'status': 'connected',
            'source': 'gpsd',
            'has_fix': False,
            'position': None
        })
    else:
        return jsonify({
            'status': 'unavailable',
            'message': 'Failed to connect to gpsd'
        })


@gps_bp.route('/stop', methods=['POST'])
def stop_gps_reader():
    """Stop GPS client."""
    reader = get_gps_reader()
    if reader:
        reader.remove_callback(_position_callback)

    stop_gps()

    return jsonify({'status': 'stopped'})


@gps_bp.route('/status')
def get_gps_status():
    """Get current GPS client status."""
    reader = get_gps_reader()

    if not reader:
        return jsonify({
            'running': False,
            'device': None,
            'position': None,
            'error': None,
            'message': 'GPS client not started'
        })

    position = reader.position
    return jsonify({
        'running': reader.is_running,
        'device': reader.device_path,
        'position': position.to_dict() if position else None,
        'last_update': reader.last_update.isoformat() if reader.last_update else None,
        'error': reader.error,
        'message': 'Waiting for GPS fix - ensure GPS has clear view of sky' if reader.is_running and not position else None
    })


@gps_bp.route('/position')
def get_position():
    """Get current GPS position."""
    position = get_current_position()

    if position:
        return jsonify({
            'status': 'ok',
            'position': position.to_dict()
        })
    else:
        reader = get_gps_reader()
        if not reader or not reader.is_running:
            return jsonify({
                'status': 'error',
                'message': 'GPS client not running'
            }), 400
        else:
            return jsonify({
                'status': 'waiting',
                'message': 'Waiting for GPS fix - ensure GPS has clear view of sky'
            })


@gps_bp.route('/stream')
def stream_gps():
    """SSE stream of GPS position updates."""
    def generate() -> Generator[str, None, None]:
        last_keepalive = time.time()
        keepalive_interval = 30.0

        while True:
            try:
                position = _gps_queue.get(timeout=1)
                last_keepalive = time.time()
                yield format_sse({'type': 'position', **position})
            except queue.Empty:
                now = time.time()
                if now - last_keepalive >= keepalive_interval:
                    yield format_sse({'type': 'keepalive'})
                    last_keepalive = now

    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response


@gps_bp.route('/manual', methods=['GET'])
def get_manual():
    """
    Get the manually configured position.

    Returns the fixed position if one has been set, otherwise returns null.
    Manual position is used as fallback when no GPS hardware is available.

    Returns:
        JSON with manual position or null.
    """
    position = get_manual_position()

    if position:
        return jsonify({
            'status': 'ok',
            'position': position.to_dict()
        })
    else:
        return jsonify({
            'status': 'ok',
            'position': None,
            'message': 'No manual position configured'
        })


@gps_bp.route('/manual', methods=['POST'])
def set_manual():
    """
    Set a manual/fixed GPS position.

    This position will be used when no GPS hardware (gpsd) is available.
    The position is persisted to the database and survives restarts.

    JSON body:
        {
            "latitude": 42.3314,     // Required: -90 to 90
            "longitude": -83.0458,   // Required: -180 to 180
            "altitude": 200          // Optional: meters above sea level
        }

    Returns:
        JSON with the saved position.
    """
    data = request.get_json(silent=True) or {}

    latitude = data.get('latitude')
    longitude = data.get('longitude')
    altitude = data.get('altitude')

    if latitude is None or longitude is None:
        return jsonify({
            'status': 'error',
            'message': 'latitude and longitude are required'
        }), 400

    try:
        latitude = float(latitude)
        longitude = float(longitude)
        if altitude is not None:
            altitude = float(altitude)
    except (TypeError, ValueError) as e:
        return jsonify({
            'status': 'error',
            'message': f'Invalid coordinate value: {e}'
        }), 400

    try:
        position = set_manual_position(latitude, longitude, altitude)
        return jsonify({
            'status': 'ok',
            'message': 'Manual position saved',
            'position': position.to_dict()
        })
    except ValueError as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 400


@gps_bp.route('/manual', methods=['DELETE'])
def delete_manual():
    """
    Clear the manual position.

    After clearing, get_current_position() will only return data from
    GPS hardware (gpsd).

    Returns:
        JSON confirmation.
    """
    clear_manual_position()
    return jsonify({
        'status': 'ok',
        'message': 'Manual position cleared'
    })
