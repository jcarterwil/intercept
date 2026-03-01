"""GPS routes for gpsd daemon support."""

from __future__ import annotations

import queue
import time
from collections.abc import Generator

from flask import Blueprint, Response, jsonify, request

from utils.gps import (
    GPSPosition,
    GPSSkyData,
    clear_manual_position,
    detect_gps_devices,
    get_current_position,
    get_gps_reader,
    get_manual_position,
    is_gpsd_running,
    set_manual_position,
    start_gpsd,
    start_gpsd_daemon,
    stop_gps,
    stop_gpsd_daemon,
)
from utils.logging import get_logger
from utils.sse import sse_stream_fanout

logger = get_logger('intercept.gps')

gps_bp = Blueprint('gps', __name__, url_prefix='/gps')

# Queue for SSE position updates
_gps_queue: queue.Queue = queue.Queue(maxsize=100)


def _position_callback(position: GPSPosition) -> None:
    """Callback to queue position updates for SSE stream."""
    try:
        _gps_queue.put_nowait({'type': 'position', **position.to_dict()})
    except queue.Full:
        # Discard oldest if queue is full
        try:
            _gps_queue.get_nowait()
            _gps_queue.put_nowait({'type': 'position', **position.to_dict()})
        except queue.Empty:
            pass


def _sky_callback(sky: GPSSkyData) -> None:
    """Callback to queue sky data updates for SSE stream."""
    try:
        _gps_queue.put_nowait({'type': 'sky', **sky.to_dict()})
    except queue.Full:
        try:
            _gps_queue.get_nowait()
            _gps_queue.put_nowait({'type': 'sky', **sky.to_dict()})
        except queue.Empty:
            pass


@gps_bp.route('/auto-connect', methods=['POST'])
def auto_connect_gps():
    """
    Automatically connect to gpsd if available.

    Called on page load to seamlessly enable GPS if gpsd is running.
    If gpsd is not running, attempts to detect GPS devices and start gpsd.
    Returns current status if already connected.
    """
    # Check if already running
    reader = get_gps_reader()
    if reader and reader.is_running:
        # Ensure stream callbacks are attached for this process.
        reader.add_callback(_position_callback)
        reader.add_sky_callback(_sky_callback)
        position = reader.position
        sky = reader.sky
        return jsonify({
            'status': 'connected',
            'source': 'gpsd',
            'has_fix': position is not None,
            'position': position.to_dict() if position else None,
            'sky': sky.to_dict() if sky else None,
        })

    host = 'localhost'
    port = 2947

    # If gpsd isn't running, try to detect a device and start it
    if not is_gpsd_running(host, port):
        devices = detect_gps_devices()
        if not devices:
            return jsonify({
                'status': 'unavailable',
                'message': 'No GPS device detected'
            })

        # Try to start gpsd with the first detected device
        device_path = devices[0]['path']
        success, msg = start_gpsd_daemon(device_path, host, port)
        if not success:
            return jsonify({
                'status': 'unavailable',
                'message': msg,
                'devices': devices,
            })
        logger.info(f"Auto-started gpsd on {device_path}")

    # Clear the queue
    while not _gps_queue.empty():
        try:
            _gps_queue.get_nowait()
        except queue.Empty:
            break

    # Start the gpsd client
    success = start_gpsd(host, port,
                         callback=_position_callback,
                         sky_callback=_sky_callback)

    if success:
        return jsonify({
            'status': 'connected',
            'source': 'gpsd',
            'has_fix': False,
            'position': None,
            'sky': None,
        })
    else:
        return jsonify({
            'status': 'unavailable',
            'message': 'Failed to connect to gpsd'
        })


@gps_bp.route('/devices')
def list_gps_devices():
    """List detected GPS serial devices."""
    devices = detect_gps_devices()
    return jsonify({
        'devices': devices,
        'gpsd_running': is_gpsd_running(),
    })


@gps_bp.route('/stop', methods=['POST'])
def stop_gps_reader():
    """Stop GPS client and gpsd daemon if we started it."""
    reader = get_gps_reader()
    if reader:
        reader.remove_callback(_position_callback)
        reader.remove_sky_callback(_sky_callback)

    stop_gps()
    stop_gpsd_daemon()

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
            'sky': None,
            'error': None,
            'message': 'GPS client not started'
        })

    position = reader.position
    sky = reader.sky
    return jsonify({
        'running': reader.is_running,
        'device': reader.device_path,
        'position': position.to_dict() if position else None,
        'sky': sky.to_dict() if sky else None,
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


@gps_bp.route('/satellites')
def get_satellites():
    """Get current satellite sky view data."""
    reader = get_gps_reader()

    if not reader or not reader.is_running:
        return jsonify({
            'status': 'waiting',
            'running': False,
            'message': 'GPS client not running'
        })

    sky = reader.sky
    if sky:
        return jsonify({
            'status': 'ok',
            'sky': sky.to_dict()
        })
    else:
        return jsonify({
            'status': 'waiting',
            'message': 'Waiting for satellite data'
        })


@gps_bp.route('/stream')
def stream_gps():
    """SSE stream of GPS position and sky updates."""
    response = Response(
        sse_stream_fanout(
            source_queue=_gps_queue,
            channel_key='gps',
            timeout=1.0,
            keepalive_interval=30.0,
        ),
        mimetype='text/event-stream',
    )
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
