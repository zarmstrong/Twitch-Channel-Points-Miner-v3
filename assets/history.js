/**
 * Optional History tab for the analytics dashboard.
 *
 * This file is self-contained on purpose: it injects its own tab button and
 * panel, wraps the existing switchDashboardTab() rather than editing it, and
 * only talks to the /history/* blueprint owned by the fork.  That keeps the
 * upstream dashboard diff to the two asset tags in charts.html.
 */
(function () {
    'use strict';

    var REFRESH_MS = 60000;
    var DAY_OPTIONS = [7, 30, 90, 365];
    var numberFormat = new Intl.NumberFormat();

    var state = {
        days: 30,
        streamer: '',
        sessionKind: 'watch',
        visible: false,
        loading: false,
        loadedAt: 0,
        timer: null,
        charts: {},
        lastDaily: null
    };

    function byId(id) {
        return document.getElementById(id);
    }

    function escapeHtml(value) {
        return String(value === null || value === undefined ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    function formatNumber(value) {
        var number = Number(value);
        return Number.isFinite(number) ? numberFormat.format(Math.round(number)) : '0';
    }

    function formatSigned(value) {
        var number = Number(value) || 0;
        return (number > 0 ? '+' : '') + formatNumber(number);
    }

    function formatDuration(seconds) {
        var total = Math.max(0, Math.round(Number(seconds) || 0));
        if (total < 60) return total + 's';
        var minutes = Math.floor(total / 60);
        var hours = Math.floor(minutes / 60);
        minutes = minutes % 60;
        if (hours <= 0) return minutes + 'm';
        return hours + 'h ' + String(minutes).padStart(2, '0') + 'm';
    }

    function formatHours(value) {
        var hours = Number(value);
        if (!Number.isFinite(hours)) return '0h';
        // Keep chart axis labels readable: at most two decimals, no float noise.
        return String(Number(hours.toFixed(2))) + 'h';
    }

    function formatTime(timestamp) {
        var value = Number(timestamp);
        if (!Number.isFinite(value) || value <= 0) return '—';
        return new Date(value * 1000).toLocaleString();
    }

    function formatDay(day) {
        return String(day || '').slice(5);
    }

    function reasonLabel(reason) {
        var labels = {
            points: 'points',
            streak: 'streak',
            drops: 'drops',
            badge: 'badge'
        };
        return labels[reason] || reason || 'points';
    }

    function eventLabel(type) {
        var labels = {
            watch_streak: 'streak',
            prediction_made: 'prediction',
            win: 'win',
            lose: 'loss',
            points: 'points'
        };
        return labels[type] || type || 'event';
    }

    function chartForeColor() {
        var checkbox = byId('dark-mode');
        return checkbox && checkbox.checked === false ? '#373d3f' : '#f5f5f5';
    }

    function chartTooltipTheme() {
        var checkbox = byId('dark-mode');
        return checkbox && checkbox.checked === false ? 'light' : 'dark';
    }

    function applyHistoryTheme() {
        var panel = byId('history-panel');
        if (!panel) return;
        var checkbox = byId('dark-mode');
        var dark = !checkbox || checkbox.checked !== false;
        panel.classList.toggle('history-dark', dark);
        // ApexCharts captures foreColor at construction, so redraw the two
        // charts when the dashboard theme changes while History is visible.
        if (state.visible && state.lastDaily) {
            renderCharts(state.lastDaily);
        }
    }

    function buildUrl(path, params) {
        var query = [];
        var payload = params || {};
        Object.keys(payload).forEach(function (key) {
            var value = payload[key];
            if (value !== undefined && value !== null && value !== '') {
                query.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
            }
        });
        return '/history/' + path + (query.length ? '?' + query.join('&') : '');
    }

    function fetchJson(path, params) {
        return fetch(buildUrl(path, params), {
            credentials: 'same-origin',
            headers: { Accept: 'application/json' }
        }).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (body) {
                if (!response.ok) {
                    throw new Error(body.error || ('HTTP ' + response.status));
                }
                return body;
            });
        });
    }

    function setStatus(message) {
        var status = byId('history-status');
        if (status) status.textContent = message || '';
    }

    function showError(message) {
        var box = byId('history-error');
        if (!box) return;
        box.textContent = message;
        box.style.display = message ? 'block' : 'none';
    }

    // -- panel / tab injection ---------------------------------------------
    function buildPanel() {
        if (byId('history-panel')) return;
        var panel = document.createElement('div');
        panel.id = 'history-panel';
        panel.style.display = 'none';
        panel.innerHTML = [
            '<div class="box history-header">',
            '  <div class="history-toolbar">',
            '    <div class="history-windows" id="history-windows"></div>',
            '    <div class="history-controls">',
            '      <label class="history-filter">Streamer',
            '        <span class="select is-small"><select id="history-streamer"></select></span>',
            '      </label>',
            '      <label class="history-filter">Sessions',
            '        <span class="select is-small"><select id="history-session-kind">',
            '          <option value="watch">watch sessions</option>',
            '          <option value="stream">live sessions</option>',
            '          <option value="run">miner runs</option>',
            '        </select></span>',
            '      </label>',
            '      <button id="history-refresh" class="button is-small" type="button">Refresh</button>',
            '    </div>',
            '  </div>',
            '  <div id="history-status" class="history-status"></div>',
            '  <div id="history-error" class="notification is-danger" role="alert" style="display:none"></div>',
            '</div>',
            '<div id="history-cards" class="history-cards"></div>',
            '<div class="history-grid">',
            '  <div class="box history-chart-box">',
            '    <h3 class="title is-5">Daily points</h3>',
            '    <div id="history-points-chart" class="history-chart"></div>',
            '  </div>',
            '  <div class="box history-chart-box">',
            '    <h3 class="title is-5">Watch time</h3>',
            '    <div id="history-watch-chart" class="history-chart"></div>',
            '  </div>',
            '</div>',
            '<div class="box history-panel-box">',
            '  <h3 class="title is-5">Streaming and watch activity</h3>',
            '  <div id="history-activity" class="history-activity"></div>',
            '</div>',
            '<div class="box history-panel-box">',
            '  <h3 class="title is-5">Streamers</h3>',
            '  <div id="history-streamers" class="history-table-wrap"></div>',
            '</div>',
            '<div class="box history-panel-box">',
            '  <h3 class="title is-5">Farming sessions</h3>',
            '  <div id="history-sessions" class="history-table-wrap"></div>',
            '</div>',
            '<div class="box history-panel-box">',
            '  <h3 class="title is-5">Recent events</h3>',
            '  <div id="history-events" class="history-events"></div>',
            '</div>',
            '<div class="box history-detail" id="history-detail" style="display:none"></div>'
        ].join('\n');

        var logsPanel = byId('logs-panel');
        if (logsPanel && logsPanel.parentNode) {
            logsPanel.parentNode.insertBefore(panel, logsPanel.nextSibling);
        } else {
            var container = document.querySelector('.container');
            if (container) container.appendChild(panel);
        }
    }

    function buildTabButton() {
        if (byId('tab-history')) return;
        var tabs = byId('dashboard-tabs');
        if (!tabs) return;
        var button = document.createElement('button');
        button.id = 'tab-history';
        button.className = 'button is-small';
        button.type = 'button';
        button.textContent = 'History';
        button.addEventListener('click', function () {
            switchDashboardTab('history');
        });
        tabs.appendChild(button);
    }

    function wrapTabSwitcher() {
        if (typeof window.switchDashboardTab !== 'function') return;
        if (window.switchDashboardTab.__historyWrapped) return;
        var baseSwitch = window.switchDashboardTab;
        var wrapped = function (tabName) {
            baseSwitch(tabName);
            var isHistory = tabName === 'history';
            var panel = byId('history-panel');
            if (panel) panel.style.display = isHistory ? 'block' : 'none';
            var button = byId('tab-history');
            if (button) {
                button.classList.toggle('is-link', isHistory);
            }
            if (isHistory) {
                showHistory();
            } else {
                hideHistory();
            }
        };
        wrapped.__historyWrapped = true;
        window.switchDashboardTab = wrapped;
    }

    // -- controls ----------------------------------------------------------
    function renderWindows() {
        var container = byId('history-windows');
        if (!container) return;
        container.innerHTML = '';
        DAY_OPTIONS.forEach(function (days) {
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'button is-small' + (state.days === days ? ' is-link' : '');
            button.textContent = days === 365 ? '1 year' : days + ' days';
            button.addEventListener('click', function () {
                if (state.days === days) return;
                state.days = days;
                renderWindows();
                refreshHistory();
            });
            container.appendChild(button);
        });
    }

    function bindControls() {
        var refresh = byId('history-refresh');
        if (refresh) {
            refresh.addEventListener('click', function () {
                refreshHistory(true);
            });
        }
        var streamer = byId('history-streamer');
        if (streamer) {
            streamer.addEventListener('change', function () {
                state.streamer = streamer.value || '';
                refreshHistory();
                var detail = byId('history-detail');
                if (detail) detail.style.display = 'none';
            });
        }
        var kind = byId('history-session-kind');
        if (kind) {
            kind.value = state.sessionKind;
            kind.addEventListener('change', function () {
                state.sessionKind = kind.value || 'watch';
                refreshHistory();
            });
        }
        var darkMode = byId('dark-mode');
        if (darkMode) {
            darkMode.addEventListener('change', applyHistoryTheme);
        }
        var streamers = byId('history-streamers');
        if (streamers) {
            streamers.addEventListener('click', function (event) {
                var link = event.target.closest('[data-history-streamer]');
                if (!link) return;
                event.preventDefault();
                loadStreamerDetail(link.getAttribute('data-history-streamer'));
            });
        }
        var detail = byId('history-detail');
        if (detail) {
            detail.addEventListener('click', function (event) {
                if (event.target.closest('[data-history-detail-close]')) {
                    detail.style.display = 'none';
                }
            });
        }
    }

    // -- show / hide / polling --------------------------------------------
    function showHistory() {
        state.visible = true;
        renderWindows();
        if (!state.loadedAt) {
            refreshHistory();
        } else if (Date.now() - state.loadedAt > REFRESH_MS) {
            refreshHistory();
        }
        if (state.timer) clearInterval(state.timer);
        state.timer = setInterval(function () {
            if (state.visible && document.visibilityState !== 'hidden') {
                refreshHistory();
            }
        }, REFRESH_MS);
    }

    function hideHistory() {
        state.visible = false;
        if (state.timer) {
            clearInterval(state.timer);
            state.timer = null;
        }
    }

    function refreshHistory(force) {
        if (state.loading) return;
        if (!state.visible && !force) return;
        state.loading = true;
        showError('');
        setStatus('Loading history…');

        var params = { days: state.days };
        var filtered = { days: state.days, streamer: state.streamer };
        Promise.all([
            fetchJson('overview', params),
            fetchJson('streamers', params),
            fetchJson('daily', filtered),
            fetchJson('sessions', {
                kind: state.sessionKind,
                days: state.days,
                streamer: state.streamer,
                limit: 100
            }),
            fetchJson('events', {
                days: state.days,
                streamer: state.streamer,
                limit: 100
            }),
            fetchJson('activity', filtered)
        ]).then(function (results) {
            renderCards(results[0]);
            renderStreamerOptions(results[1]);
            renderStreamers(results[1]);
            state.lastDaily = results[2];
            renderCharts(results[2]);
            renderSessions(results[3]);
            renderEvents(results[4]);
            renderActivity(results[5]);
            state.loadedAt = Date.now();
            setStatus('Updated ' + new Date().toLocaleTimeString());
        }).catch(function (error) {
            var message = error && error.message ? error.message : String(error);
            if (/not available/i.test(message)) {
                setStatus('History recorder is starting…');
                showError('');
                setTimeout(function () {
                    if (state.visible) refreshHistory();
                }, 5000);
            } else {
                showError(message);
                setStatus('');
            }
        }).finally(function () {
            state.loading = false;
        });
    }

    // -- rendering ---------------------------------------------------------
    function renderCards(overview) {
        var container = byId('history-cards');
        if (!container) return;
        var windowStats = overview.window || {};
        var today = overview.today || {};
        var lifetime = overview.lifetime || {};
        var run = overview.current_run;
        var cards = [
            {
                label: 'Points gained',
                value: formatSigned(windowStats.points_gained),
                meta: 'Today ' + formatSigned(today.points_gained) +
                    ' · All-time ' + formatSigned(lifetime.points_gained)
            },
            {
                label: 'Points spent',
                value: formatNumber(windowStats.points_spent),
                meta: 'Predictions, rewards and redemptions'
            },
            {
                label: 'Watch time',
                value: formatDuration(windowStats.watch_seconds),
                meta: 'Today ' + formatDuration(today.watch_seconds) +
                    ' · All-time ' + formatDuration(lifetime.watch_seconds)
            },
            {
                label: 'Streak hits',
                value: formatNumber(window.streak_count),
                meta: 'Today ' + formatNumber(today.streak_count)
            },
            {
                label: 'Active now',
                value: formatNumber(overview.active_watch_sessions),
                meta: overview.live_streams + ' live · ' +
                    (run ? 'run ' + formatDuration((Date.now() / 1000) - run.started_at) : 'no run')
            }
        ];
        container.innerHTML = cards.map(function (card) {
            return '<div class="history-card">' +
                '<p class="history-card-label">' + escapeHtml(card.label) + '</p>' +
                '<p class="history-card-value">' + escapeHtml(card.value) + '</p>' +
                '<p class="history-card-meta">' + escapeHtml(card.meta) + '</p>' +
                '</div>';
        }).join('');
    }

    function renderStreamerOptions(streamersPayload) {
        var select = byId('history-streamer');
        if (!select) return;
        var names = (streamersPayload.streamers || []).map(function (item) {
            return item.streamer;
        }).sort();
        var current = state.streamer;
        var options = ['<option value="">All streamers</option>'];
        names.forEach(function (name) {
            options.push('<option value="' + escapeHtml(name) + '">' +
                escapeHtml(name) + '</option>');
        });
        select.innerHTML = options.join('');
        if (names.indexOf(current) === -1 && current) {
            state.streamer = '';
        }
        select.value = state.streamer;
    }

    function destroyChart(name) {
        if (state.charts[name]) {
            try { state.charts[name].destroy(); } catch (error) { /* ignore */ }
            state.charts[name] = null;
        }
    }

    function renderCharts(dailyPayload) {
        var rows = (dailyPayload && dailyPayload.daily) || [];
        if (typeof ApexCharts === 'undefined') return;
        var categories = rows.map(function (row) { return row.day; });
        var gains = rows.map(function (row) { return Number(row.points_gained) || 0; });
        var spent = rows.map(function (row) { return -(Number(row.points_spent) || 0); });
        var watchHours = rows.map(function (row) {
            return Number(((Number(row.watch_seconds) || 0) / 3600).toFixed(2));
        });
        var foreColor = chartForeColor();

        destroyChart('points');
        var pointsEl = byId('history-points-chart');
        if (pointsEl) {
            state.charts.points = new ApexCharts(pointsEl, {
                chart: {
                    type: 'bar',
                    height: 260,
                    toolbar: { show: false },
                    foreColor: foreColor
                },
                series: [
                    { name: 'Gained', data: gains },
                    { name: 'Spent / lost', data: spent }
                ],
                colors: ['#3b82f6', '#ef4444'],
                plotOptions: { bar: { columnWidth: '65%' } },
                dataLabels: { enabled: false },
                xaxis: { categories: categories, labels: { rotate: -45 } },
                legend: { position: 'top' },
                tooltip: {
                    theme: chartTooltipTheme(),
                    y: { formatter: function (value) { return formatNumber(value); } }
                },
                noData: { text: 'No points recorded in this window.' }
            });
            state.charts.points.render();
        }

        destroyChart('watch');
        var watchEl = byId('history-watch-chart');
        if (watchEl) {
            state.charts.watch = new ApexCharts(watchEl, {
                chart: {
                    type: 'area',
                    height: 260,
                    toolbar: { show: false },
                    foreColor: foreColor
                },
                series: [{ name: 'Watched', data: watchHours }],
                colors: ['#10b981'],
                dataLabels: { enabled: false },
                stroke: { curve: 'smooth', width: 2 },
                fill: { opacity: 0.25 },
                xaxis: { categories: categories, labels: { rotate: -45 } },
                yaxis: { labels: { formatter: function (value) { return formatHours(value); } } },
                tooltip: {
                    theme: chartTooltipTheme(),
                    y: {
                        formatter: function (value, opts) {
                            var index = opts && typeof opts.dataPointIndex === 'number'
                                ? opts.dataPointIndex
                                : -1;
                            var row = rows[index];
                            if (row) return formatDuration(row.watch_seconds);
                            return formatDuration(value * 3600);
                        }
                    }
                },
                noData: { text: 'No watch time recorded in this window.' }
            });
            state.charts.watch.render();
        }
    }

    function renderActivity(activityPayload) {
        var container = byId('history-activity');
        if (!container) return;
        var days = (activityPayload && activityPayload.day_list) || [];
        var streamers = (activityPayload && activityPayload.streamers) || [];
        if (!days.length || !streamers.length) {
            container.innerHTML = '<p class="history-empty">No activity recorded yet.</p>';
            return;
        }
        var maxWatch = 1;
        var maxLive = 1;
        streamers.forEach(function (item) {
            Object.keys(item.days || {}).forEach(function (day) {
                var cell = item.days[day];
                maxWatch = Math.max(maxWatch, Number(cell.watch_seconds) || 0);
                maxLive = Math.max(maxLive, Number(cell.live_seconds) || 0);
            });
        });

        var header = ['<th class="history-heatmap-streamer">Streamer</th>'];
        days.forEach(function (day) {
            header.push('<th title="' + escapeHtml(day) + '">' +
                escapeHtml(formatDay(day)) + '</th>');
        });

        var body = streamers.map(function (item) {
            var cells = ['<td class="history-heatmap-streamer">' +
                escapeHtml(item.streamer) + '</td>'];
            days.forEach(function (day) {
                var cell = (item.days || {})[day] || {};
                var watch = Number(cell.watch_seconds) || 0;
                var live = Number(cell.live_seconds) || 0;
                var strength = watch > 0
                    ? 0.12 + (0.78 * Math.min(1, watch / maxWatch))
                    : (live > 0 ? 0.06 + (0.35 * Math.min(1, live / maxLive)) : 0);
                var text = watch > 0
                    ? (watch >= 3600
                        ? (watch / 3600).toFixed(1) + 'h'
                        : Math.max(1, Math.round(watch / 60)) + 'm')
                    : (live > 0 ? '•' : '');
                var title = day + '\nWatched: ' + formatDuration(watch) +
                    '\nLive: ' + formatDuration(live) +
                    '\nPoints: ' + formatSigned(cell.points_gained || 0) +
                    '\nStreaks: ' + formatNumber(cell.streak_count || 0);
                cells.push(
                    '<td class="history-heatmap-cell" title="' + escapeHtml(title) + '"' +
                    ' style="background:rgba(59,130,246,' + strength.toFixed(3) + ')">' +
                    escapeHtml(text) + '</td>'
                );
            });
            return '<tr>' + cells.join('') + '</tr>';
        });

        container.innerHTML = '<div class="history-heatmap-scroll"><table class="history-heatmap">' +
            '<thead><tr>' + header.join('') + '</tr></thead>' +
            '<tbody>' + body.join('') + '</tbody></table></div>';
    }

    function renderStreamers(streamersPayload) {
        var container = byId('history-streamers');
        if (!container) return;
        var items = (streamersPayload.streamers || []).slice();
        if (state.streamer) {
            items = items.filter(function (item) {
                return item.streamer === state.streamer;
            });
        }
        if (!items.length) {
            container.innerHTML = '<p class="history-empty">No streamer history in this window.</p>';
            return;
        }
        var rows = items.map(function (item) {
            var badges = [];
            if (item.live_stream) {
                badges.push('<span class="tag is-success is-light">live</span>');
            }
            if (item.active_watch) {
                badges.push('<span class="tag is-info is-light">watching: ' +
                    escapeHtml(reasonLabel(item.active_watch.reason)) + '</span>');
            }
            if (item.live_streak_at) {
                badges.push('<span class="tag is-warning is-light" title="Streak claimed this stream: ' +
                    escapeHtml(formatTime(item.live_streak_at)) + '">streak</span>');
            }
            return '<tr>' +
                '<td><a href="#" data-history-streamer="' + escapeHtml(item.streamer) + '">' +
                escapeHtml(item.streamer) + '</a> ' + badges.join(' ') + '</td>' +
                '<td class="num">' + escapeHtml(formatSigned(item.points_gained)) + '</td>' +
                '<td class="num">' + escapeHtml(formatNumber(item.points_spent)) + '</td>' +
                '<td class="num">' + escapeHtml(formatDuration(item.watch_seconds)) + '</td>' +
                '<td class="num">' + escapeHtml(formatNumber(item.watch_sessions)) + '</td>' +
                '<td class="num">' + escapeHtml(formatNumber(item.stream_sessions)) + '</td>' +
                '<td class="num">' + escapeHtml(formatNumber(item.streak_count)) + '</td>' +
                '<td class="history-muted">' + escapeHtml(formatTime(item.last_seen)) + '</td>' +
                '</tr>';
        }).join('');
        container.innerHTML = '<table class="table is-fullwidth is-narrow is-hoverable history-table">' +
            '<thead><tr><th>Streamer</th><th class="num">Gained</th>' +
            '<th class="num">Spent</th><th class="num">Watch</th>' +
            '<th class="num">Sessions</th><th class="num">Live</th>' +
            '<th class="num">Streaks</th><th>Last seen</th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table>';
    }

    function renderSessions(sessionsPayload) {
        var container = byId('history-sessions');
        if (!container) return;
        var items = (sessionsPayload && sessionsPayload.sessions) || [];
        var kind = (sessionsPayload && sessionsPayload.kind) || state.sessionKind;
        if (!items.length) {
            container.innerHTML = '<p class="history-empty">No sessions recorded in this window.</p>';
            return;
        }
        var rows = items.map(function (item) {
            var name = kind === 'run' ? 'miner run' : (item.streamer || '');
            var info = kind === 'watch'
                ? reasonLabel(item.reason)
                : (kind === 'stream'
                    ? [item.game, item.title].filter(Boolean).join(' · ') || '—'
                    : item.version || '—');
            return '<tr' + (item.active ? ' class="history-active-row"' : '') + '>' +
                '<td>' + escapeHtml(name) + '</td>' +
                '<td>' + escapeHtml(info) + '</td>' +
                '<td class="history-muted">' + escapeHtml(formatTime(item.started_at)) + '</td>' +
                '<td class="num">' + escapeHtml(formatDuration(item.duration_seconds)) + '</td>' +
                '<td>' + (item.active
                    ? '<span class="tag is-success is-light">active</span>'
                    : '<span class="history-muted">' + escapeHtml(formatTime(item.ended_at)) + '</span>') +
                '</td></tr>';
        }).join('');
        container.innerHTML = '<p class="history-summary-line">Showing ' + items.length +
            ' of ' + formatNumber(sessionsPayload.total) + ' in this window.</p>' +
            '<table class="table is-fullwidth is-narrow history-table">' +
            '<thead><tr><th>' + (kind === 'run' ? 'Run' : 'Streamer') + '</th>' +
            '<th>' + (kind === 'watch' ? 'Reason' : (kind === 'stream' ? 'Stream' : 'Version')) + '</th>' +
            '<th>Started</th><th class="num">Duration</th><th>Status</th></tr></thead>' +
            '<tbody>' + rows + '</tbody></table>';
    }

    function renderEvents(eventsPayload) {
        var container = byId('history-events');
        if (!container) return;
        var items = (eventsPayload && eventsPayload.events) || [];
        if (!items.length) {
            container.innerHTML = '<p class="history-empty">No events recorded in this window.</p>';
            return;
        }
        container.innerHTML = items.map(function (item) {
            return '<div class="history-event">' +
                '<span class="tag history-event-tag history-event-' +
                escapeHtml(item.type) + '">' + escapeHtml(eventLabel(item.type)) + '</span>' +
                '<span class="history-event-streamer">' + escapeHtml(item.streamer) + '</span>' +
                '<span class="history-event-label">' + escapeHtml(item.label || '') + '</span>' +
                '<span class="history-event-time">' + escapeHtml(formatTime(item.ts)) + '</span>' +
                '</div>';
        }).join('');
    }

    function loadStreamerDetail(streamer) {
        var container = byId('history-detail');
        if (!container) return;
        container.style.display = 'block';
        container.innerHTML = '<p class="history-empty">Loading ' + escapeHtml(streamer) + '…</p>';
        fetchJson('streamer/' + encodeURIComponent(streamer), { days: state.days })
            .then(function (detail) {
                renderStreamerDetail(detail);
                container.scrollIntoView({ behavior: 'smooth', block: 'start' });
            })
            .catch(function (error) {
                container.innerHTML = '<div class="notification is-danger">' +
                    escapeHtml(error.message || String(error)) + '</div>';
            });
    }

    function renderStreamerDetail(detail) {
        var container = byId('history-detail');
        if (!container) return;
        var summary = detail.summary || {};
        var lifetime = summary.lifetime || {};
        var daily = detail.daily || [];
        var totalGained = daily.reduce(function (sum, row) {
            return sum + (Number(row.points_gained) || 0);
        }, 0);
        var totalWatch = daily.reduce(function (sum, row) {
            return sum + (Number(row.watch_seconds) || 0);
        }, 0);
        var totalStreaks = daily.reduce(function (sum, row) {
            return sum + (Number(row.streak_count) || 0);
        }, 0);

        var sessionRows = (detail.watch_sessions || []).slice(0, 25).map(function (session) {
            return '<tr><td>' + escapeHtml(reasonLabel(session.reason)) + '</td>' +
                '<td class="history-muted">' + escapeHtml(formatTime(session.started_at)) + '</td>' +
                '<td class="num">' + escapeHtml(formatDuration(session.watch_seconds)) + '</td>' +
                '<td>' + (session.ended_at === null
                    ? '<span class="tag is-success is-light">active</span>'
                    : '<span class="history-muted">' + escapeHtml(formatTime(session.ended_at)) + '</span>') +
                '</td></tr>';
        }).join('');

        var streamRows = (detail.streams || []).slice(0, 15).map(function (session) {
            var end = session.ended_at || session.last_seen_at || (Date.now() / 1000);
            return '<tr><td class="history-muted">' + escapeHtml(formatTime(session.started_at)) + '</td>' +
                '<td class="num">' + escapeHtml(formatDuration(end - session.started_at)) + '</td>' +
                '<td>' + (session.ended_at === null
                    ? '<span class="tag is-success is-light">live</span>'
                    : '<span class="history-muted">' + escapeHtml(formatTime(session.ended_at)) + '</span>') +
                '</td><td>' + escapeHtml([session.game, session.title].filter(Boolean).join(' · ') || '—') +
                '</td></tr>';
        }).join('');

        var streakRows = (detail.events || []).filter(function (event) {
            return event.type === 'watch_streak';
        }).slice(0, 20).map(function (event) {
            return '<tr><td class="history-muted">' + escapeHtml(formatTime(event.ts)) + '</td>' +
                '<td>' + escapeHtml(event.label || 'Watch streak') + '</td></tr>';
        }).join('');

        container.innerHTML = [
            '<div class="history-detail-head">',
            '  <h3 class="title is-4">' + escapeHtml(detail.streamer) + '</h3>',
            '  <button class="button is-small" type="button" data-history-detail-close>Close</button>',
            '</div>',
            '<div class="history-cards">',
            '  <div class="history-card"><p class="history-card-label">Window gained</p><p class="history-card-value">' +
                escapeHtml(formatSigned(totalGained)) + '</p><p class="history-card-meta">All-time ' +
                escapeHtml(formatSigned(lifetime.points_gained)) + '</p></div>',
            '  <div class="history-card"><p class="history-card-label">Window watch time</p><p class="history-card-value">' +
                escapeHtml(formatDuration(totalWatch)) + '</p><p class="history-card-meta">All-time ' +
                escapeHtml(formatDuration(lifetime.watch_seconds)) + '</p></div>',
            '  <div class="history-card"><p class="history-card-label">Window streaks</p><p class="history-card-value">' +
                escapeHtml(formatNumber(totalStreaks)) + '</p><p class="history-card-meta">Last ' +
                escapeHtml(formatTime(summary.last_streak_at)) + '</p></div>',
            '  <div class="history-card"><p class="history-card-label">Sessions</p><p class="history-card-value">' +
                escapeHtml(formatNumber(summary.watch_sessions)) + '</p><p class="history-card-meta">' +
                escapeHtml(formatNumber(summary.stream_sessions)) + ' live sessions</p></div>',
            '</div>',
            '<div class="history-detail-grid">',
            '  <div><h4 class="title is-6">Recent watch sessions</h4>' +
                (sessionRows
                    ? '<div class="history-table-wrap history-detail-table"><table class="table is-fullwidth is-narrow history-table"><thead><tr><th>Reason</th><th>Started</th><th class="num">Watched</th><th>Ended</th></tr></thead><tbody>' +
                        sessionRows + '</tbody></table></div>'
                    : '<p class="history-empty">No watch sessions.</p>') + '</div>',
            '  <div><h4 class="title is-6">Recent live sessions</h4>' +
                (streamRows
                    ? '<div class="history-table-wrap history-detail-table"><table class="table is-fullwidth is-narrow history-table"><thead><tr><th>Started</th><th class="num">Duration</th><th>Status</th><th>Stream</th></tr></thead><tbody>' +
                        streamRows + '</tbody></table></div>'
                    : '<p class="history-empty">No live sessions.</p>') + '</div>',
            '</div>',
            '<h4 class="title is-6">Streak history</h4>',
            streakRows
                ? '<div class="history-table-wrap history-detail-table"><table class="table is-fullwidth is-narrow history-table"><tbody>' +
                    streakRows + '</tbody></table></div>'
                : '<p class="history-empty">No streak events yet.</p>'
        ].join('\n');
    }

    // -- install -----------------------------------------------------------
    buildPanel();
    buildTabButton();
    wrapTabSwitcher();

    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && state.visible) {
            refreshHistory();
        }
    });

    if (typeof window.jQuery !== 'undefined') {
        window.jQuery(document).ready(function () {
            bindControls();
            renderWindows();
            applyHistoryTheme();
            try {
                if (window.location.hash === '#history') {
                    switchDashboardTab('history');
                } else if (window.safeStorage &&
                           window.safeStorage.getItem('dashboardTab') === 'history') {
                    switchDashboardTab('history');
                }
            } catch (error) {
                showError('Unable to open the History tab: ' + error.message);
            }
        });
    }
}());
