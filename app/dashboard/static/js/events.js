/* Agent Core — SSE live event connection */

(function () {
  var _es = null;
  var _delay = 1000;
  var _maxDelay = 30000;
  var _timer = null;

  var _eventTypes = [
    'activity_created',
    'activity_updated',
    'activity_heartbeat',
    'activity_cancelled',
    'activity_recovered',
    'connector_executed',
  ];

  function connect() {
    if (_es) return;
    _es = new EventSource('/api/events');

    _es.onopen = function () {
      _delay = 1000;
    };

    _eventTypes.forEach(function (type) {
      _es.addEventListener(type, dispatch);
    });

    _es.onerror = function () {
      _es.close();
      _es = null;
      clearTimeout(_timer);
      _timer = setTimeout(function () {
        _delay = Math.min(_delay * 2, _maxDelay);
        connect();
      }, _delay);
    };
  }

  function dispatch(e) {
    var payload;
    try {
      payload = JSON.parse(e.data);
    } catch (_) {
      return;
    }
    var handlerName = window.AGENT_CORE_WINDOW_EVENT || 'onAgentCoreEvent';
    if (typeof window[handlerName] === 'function') {
      try {
        Promise.resolve(window[handlerName](payload)).catch(function () {});
      } catch (_) {}
    }
  }

  document.addEventListener('DOMContentLoaded', connect);
})();
