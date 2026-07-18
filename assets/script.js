// https://apexcharts.com/javascript-chart-demos/line-charts/zoomable-timeseries/
var options = {
    series: [],
    chart: {
        type: 'area',
        stacked: false,
        height: 490,
        zoom: {
            type: 'x',
            enabled: true,
            autoScaleYaxis: true
        },
        // background: '#2B2D3E',
        foreColor: '#fff'
    },
    dataLabels: {
        enabled: false
    },
    stroke: {
        curve: 'smooth',
    },
    markers: {
        size: 0,
    },
    title: {
        text: 'Channel points (dates are displayed in UTC)',
        align: 'left'
    },
    colors: ["#f9826c"],
    fill: {
        type: 'gradient',
        gradient: {
            shadeIntensity: 1,
            inverseColors: false,
            opacityFrom: 0.5,
            opacityTo: 0,
            stops: [0, 90, 100]
        },
    },
    yaxis: {
        title: {
            text: 'Channel points'
        },
    },
    xaxis: {
        type: 'datetime',
        labels: {
            datetimeUTC: false,
            format: dateFormat
        }
    },
    tooltip: {
        theme: 'dark',
        shared: false,
        x: {
            show: true,
            format: `${dateFormat} HH:mm:ss`,
        },
        custom: ({
            series,
            seriesIndex,
            dataPointIndex,
            w
        }) => {
            return (`<div class="apexcharts-active">
                <div class="apexcharts-tooltip-title">${w.globals.seriesNames[seriesIndex]}</div>
                <div class="apexcharts-tooltip-series-group apexcharts-active" style="order: 1; display: flex; padding-bottom: 0px !important;">
                    <div class="apexcharts-tooltip-text">
                        <div class="apexcharts-tooltip-y-group">
                            <span class="apexcharts-tooltip-text-label"><b>Points</b>: ${series[seriesIndex][dataPointIndex]}</span><br>
                            <span class="apexcharts-tooltip-text-label"><b>Reason</b>: ${w.globals.seriesZ[seriesIndex][dataPointIndex] ? w.globals.seriesZ[seriesIndex][dataPointIndex] : ''}</span>
                        </div>
                    </div>
                </div>
                </div>`)
        }
    },
    noData: {
        text: 'Loading...'
    }
};

var chart = new ApexCharts(document.querySelector("#chart"), options);
var chartRendered = false;
var currentStreamer = null;
var pointSeries = [];
var annotations = [];
var streamerRefreshTimeout = null;
var streamerDataRequest = 0;

var streamersList = [];
var sortBy = "Name ascending";
var sortField = 'name';
var dropsRefreshTimeout = null;
var dropsState = { categories: {}, orderedCategories: [] };
var currentDropCategory = null;
var dropsFilter = 'active';
var dropsPage = 1;
var dropsPerPage = 10;
var streamerDeleteSelectionMode = false;
var selectedStreamerAnalytics = new Set();
var lastStreamerCheckboxIndex = null;
var pendingDeleteStreamers = [];
var analyticsDeleteInProgress = false;

function showAnalyticsLoadError(message, details) {
    console.error(`[analytics] ${message}`, details || '');
    $('#analytics-load-error').text(message).show();
}

function switchDashboardTab(tabName) {
    var isPoints = tabName !== 'drops';
    $('#points-panel').toggle(isPoints);
    $('#drops-panel').toggle(!isPoints);

    $('#tab-points').toggleClass('is-link', isPoints);
    $('#tab-drops').toggleClass('is-link', !isPoints);

    localStorage.setItem('dashboardTab', isPoints ? 'points' : 'drops');

    // ApexCharts cannot reliably place annotations while its panel is hidden.
    // Reapply them after Points becomes visible, including when the page was
    // refreshed with the Drops tab saved in localStorage.
    if (isPoints && chartRendered) {
        window.requestAnimationFrame(function () {
            renderPointsChart();
            window.dispatchEvent(new Event('resize'));
        });
    }
}

var startDate = new Date();
startDate.setDate(startDate.getDate() - daysAgo);
var endDate = new Date();

$(document).ready(function () {
    var savedDarkMode = localStorage.getItem('dark-mode');
    if (savedDarkMode === null) {
        savedDarkMode = 'true';
        localStorage.setItem('dark-mode', savedDarkMode);
    }
    $('#dark-mode').prop('checked', savedDarkMode === 'true');
    $('#dark-theme').prop('disabled', savedDarkMode !== 'true');

    var savedDashboardTab = localStorage.getItem('dashboardTab') || 'points';
    dropsFilter = localStorage.getItem('dropsFilter') || 'active';
    $('#drops-filter').val(dropsFilter);

    // Variable to keep track of whether log checkbox is checked
    if (!localStorage.getItem('log-enabled')) localStorage.setItem('log-enabled', true);
    var isLogCheckboxChecked = localStorage.getItem('log-enabled') === 'true';
    $('#log').prop('checked', isLogCheckboxChecked);
    $('#log-box').toggle(isLogCheckboxChecked);

    // Variable to keep track of whether auto-update log is active
    var autoUpdateLog = true;

    // Variable to keep track of the last received log index
    var lastReceivedLogIndex = 0;
    var initialLogTailBytes = 128 * 1024;

    $('#auto-update-log').click(() => {
        autoUpdateLog = !autoUpdateLog;
        $('#auto-update-log').text(autoUpdateLog ? '⏸️' : '▶️');

        if (autoUpdateLog) {
            getLog();
        }
    });

    $('#log').change(function () {
        isLogCheckboxChecked = $(this).prop('checked');
        localStorage.setItem('log-enabled', isLogCheckboxChecked);
        $('#log-box').toggle(isLogCheckboxChecked);

        if (isLogCheckboxChecked) {
            getLog();
        }
    });

    if (isLogCheckboxChecked) {
        getLog();
    }

    // Load a recent tail first, then request only entries appended after it.
    function getLog() {
        if (isLogCheckboxChecked) {
            $.get(`/log?lastIndex=${lastReceivedLogIndex}&tailBytes=${initialLogTailBytes}`, function (data, _status, xhr) {
                // Process and display the new log entries received
                // Logs contain Twitch-controlled text (for example prediction
                // titles), so never interpret them as HTML.
                $("#log-content").append(document.createTextNode(data));
                // Scroll to the bottom of the log content
                $("#log-content").scrollTop($("#log-content")[0].scrollHeight);

                // Update the last received log index
                const nextPosition = Number(xhr.getResponseHeader("X-Log-Position"));
                if (Number.isSafeInteger(nextPosition) && nextPosition >= 0) {
                    lastReceivedLogIndex = nextPosition;
                }

                if (autoUpdateLog) {
                    setTimeout(getLog, logPollInterval);
                }
            });
        }
    }

    // Retrieve the saved header visibility preference from localStorage
    var headerVisibility = localStorage.getItem('headerVisibility');

    // Set the initial header visibility based on the saved preference or default to 'visible'
    if (headerVisibility === 'hidden') {
        $('#toggle-header').prop('checked', false);
        $('#header').hide();
        $('body').addClass('header-hidden');
    } else {
        $('#toggle-header').prop('checked', true);
        $('#header').show();
        $('body').removeClass('header-hidden');
    }

    // Handle the toggle header change event
    $('#toggle-header').change(function () {
        if (this.checked) {
            $('#header').show();
            $('body').removeClass('header-hidden');
            // Save the header visibility preference as 'visible' in localStorage
            localStorage.setItem('headerVisibility', 'visible');
        } else {
            $('#header').hide();
            $('body').addClass('header-hidden');
            // Save the header visibility preference as 'hidden' in localStorage
            localStorage.setItem('headerVisibility', 'hidden');
        }
    });

    // ApexCharts must initialize while its container is visible. If the saved
    // tab is Drops, hide Points only after chart initialization completes.
    chart.render().then(function () {
        chartRendered = true;
        switchDashboardTab(savedDashboardTab);
    });

    if (!localStorage.getItem("annotations")) localStorage.setItem("annotations", true);
    if (!localStorage.getItem("sort-by")) localStorage.setItem("sort-by", "Name ascending");

    // Restore settings from localStorage on page load
    $('#annotations').prop("checked", localStorage.getItem("annotations") === "true");

    // Handle the annotation toggle click event
    $('#annotations').click(() => {
        var isChecked = $('#annotations').prop("checked");
        localStorage.setItem("annotations", isChecked);
        updateAnnotations();
    });

    // Handle the dark mode toggle click event
    $('#dark-mode').click(() => {
        var isChecked = $('#dark-mode').prop("checked");
        localStorage.setItem("dark-mode", isChecked);
        toggleDarkMode();
    });

    $('#startDate').val(formatDate(startDate));
    $('#endDate').val(formatDate(endDate));

    $('#delete-streamer-analytics').click(toggleStreamerAnalyticsDeletion);
    $('#cancel-streamer-analytics-selection').click(function () {
        setStreamerDeleteSelectionMode(false);
    });
    $('#analytics-delete-modal-cancel').click(closeAnalyticsDeleteModal);
    $('#analytics-delete-modal-confirm').click(confirmStreamerAnalyticsDeletion);
    $('#analytics-delete-modal').click(function (event) {
        if (event.target === this) closeAnalyticsDeleteModal();
    });
    $(document).keydown(function (event) {
        if (event.key === 'Escape') closeAnalyticsDeleteModal();
    });

    sortBy = localStorage.getItem("sort-by");
    if (sortBy.includes("Points")) sortField = 'points';
    else if (sortBy.includes("Last activity")) sortField = 'last_activity';
    else sortField = 'name';
    $('#sorting-by').text(sortBy);
    getStreamers();
    getDropsByCategory();

    updateAnnotations();
    toggleDarkMode();

    // Retrieve log checkbox state from localStorage and update UI accordingly
    var logCheckboxState = localStorage.getItem('logCheckboxState');
    $('#log').prop('checked', logCheckboxState === 'true');
    if (logCheckboxState === 'true') {
        isLogCheckboxChecked = true;
        $('#auto-update-log').show();
        $('#log-box').show();
        // Start continuously updating the log content
        getLog();
    }

    // Handle the log checkbox change event
    $('#log').change(function () {
        isLogCheckboxChecked = $(this).prop('checked');
        localStorage.setItem('logCheckboxState', isLogCheckboxChecked);

        if (isLogCheckboxChecked) {
            $('#log-box').show();
            $('#auto-update-log').show();
            getLog();
            $('html, body').scrollTop($(document).height());
        } else {
            $('#log-box').hide();
            $('#auto-update-log').hide();
            // Clear log content when checkbox is unchecked
            // $("#log-content").text('');
        }
    });
});

function formatDate(date) {
    var d = new Date(date),
        month = '' + (d.getMonth() + 1),
        day = '' + d.getDate(),
        year = d.getFullYear();

    if (month.length < 2) month = '0' + month;
    if (day.length < 2) day = '0' + day;

    return [year, month, day].join('-');
}

function formatDisplayDate(date) {
    var d = new Date(date);
    if (isNaN(d.getTime())) return '';

    var values = {
        yyyy: String(d.getFullYear()),
        yy: String(d.getFullYear()).slice(-2),
        mm: String(d.getMonth() + 1).padStart(2, '0'),
        dd: String(d.getDate()).padStart(2, '0')
    };
    return dateFormat.replace(/yyyy|yy|mm|dd/g, function (token) {
        return values[token];
    });
}

function changeStreamer(streamer, index) {
    if (!streamer) {
        currentStreamer = null;
        pointSeries = [];
        annotations = [];
        localStorage.removeItem("selectedStreamer");
        updateStreamerDeleteControls();
        options.title.text = 'Channel points (dates are displayed in UTC)';
        renderPointsChart();
        return;
    }

    $("li").removeClass("is-active")
    $("li").eq(index - 1).addClass('is-active');
    currentStreamer = streamer;
    updateStreamerDeleteControls();

    // Update the chart title with the current streamer's name
    options.title.text = `${streamer.replace(".json", "")}'s channel points (dates are displayed in UTC)`;
    if (chartRendered && !$('#points-panel').is(':hidden')) {
        chart.updateOptions({ title: options.title }, false, false);
    }

    // Save the selected streamer in localStorage
    localStorage.setItem("selectedStreamer", currentStreamer);

    getStreamerData(streamer);
}

function getStreamerData(streamer) {
    if (streamerRefreshTimeout) {
        clearTimeout(streamerRefreshTimeout);
        streamerRefreshTimeout = null;
    }

    if (streamer && currentStreamer == streamer) {
        var request = ++streamerDataRequest;
        $.getJSON(`./json/${streamer}`, {
            startDate: formatDate(startDate),
            endDate: formatDate(endDate)
        }, function (response) {
            // Ignore a response for a range or streamer that has since changed.
            if (request !== streamerDataRequest || currentStreamer !== streamer) return;

            pointSeries = response["series"] || [];
            annotations = response["annotations"];
            renderPointsChart();
            streamerRefreshTimeout = setTimeout(function () {
                getStreamerData(streamer);
            }, 300000); // 5 minutes
        });
    }
}

function getAllStreamersData() {
    $.getJSON(`./json_all`, function (response) {
        for (var i in response) {
            chart.appendSeries({
                name: response[i]["name"].replace(".json", ""),
                data: response[i]["data"]["series"]
            }, true)
        }
    });
}

function getStreamers() {
    $.getJSON('streamers', function (response) {
        console.debug('[analytics] Points response', response);
        streamersList = response;
        sortStreamers();
        var availableStreamers = new Set(streamersList.map(streamer => streamer.name));
        selectedStreamerAnalytics = new Set(
            Array.from(selectedStreamerAnalytics).filter(streamer => availableStreamers.has(streamer))
        );
        if (streamersList.length === 0) streamerDeleteSelectionMode = false;

        // Restore the selected streamer from localStorage on page load
        var selectedStreamer = localStorage.getItem("selectedStreamer");

        if (selectedStreamer && streamersList.some(streamer => streamer.name === selectedStreamer)) {
            currentStreamer = selectedStreamer;
        } else {
            // If no selected streamer is found, default to the first streamer in the list
            currentStreamer = streamersList.length > 0 ? streamersList[0].name : null;
            if (currentStreamer) localStorage.setItem("selectedStreamer", currentStreamer);
            else localStorage.removeItem("selectedStreamer");
        }

        // Ensure the selected streamer is still active and scrolled into view
        renderStreamers();
    }).fail(function (xhr, status, error) {
        showAnalyticsLoadError(
            `Points failed to load (${xhr.status || status}): ${error || xhr.responseText || 'Unknown error'}`,
            xhr.responseText
        );
    });
}

function renderStreamers() {
    $("#streamers-list").empty();
    streamersList.forEach((streamer, index) => {
        var isActive = currentStreamer === streamer.name;
        if (!isActive && localStorage.getItem("selectedStreamer") === null && index === 0) {
            isActive = true;
            currentStreamer = streamer.name;
        }

        var listItem = $('<li>').attr('id', `streamer-${index}`).toggleClass('is-active', isActive);
        var row = $('<div>').addClass('streamer-list-row');
        if (streamerDeleteSelectionMode) {
            var checkbox = $('<input>')
                .attr({
                    type: 'checkbox',
                    'aria-label': `Select ${streamer.name.replace(".json", "")} analytics data`,
                    'data-index': index
                })
                .addClass('streamer-delete-checkbox')
                .prop('checked', selectedStreamerAnalytics.has(streamer.name))
                .click(function (event) {
                    handleStreamerAnalyticsSelection(event, index, streamer.name);
                });
            row.append(checkbox);
        }

        var streamerLink = $('<a>').attr('href', '#').click(function (event) {
            event.preventDefault();
            changeStreamer(streamer.name, index + 1);
        });
        streamerLink.append(
            $('<span>').addClass('streamer-name').text(streamer.name.replace(".json", ""))
        );
        if (sortField == 'points') {
            streamerLink.append($('<span>').addClass('streamer-sort-value').text(streamer.points));
        } else if (sortField == 'last_activity') {
            streamerLink.append(
                $('<span>').addClass('streamer-sort-value').text(formatDisplayDate(streamer.last_activity))
            );
        }
        row.append(streamerLink);
        listItem.append(row);
        $("#streamers-list").append(listItem);
        if (isActive) {
            // Scroll the selected streamer into view
            listItem[0].scrollIntoView({
                behavior: 'smooth',
                block: 'center'
            });
        }
    });

    changeStreamer(currentStreamer, streamersList.findIndex(streamer => streamer.name === currentStreamer) + 1);
    updateStreamerDeleteControls();
}

function toggleStreamerAnalyticsDeletion() {
    if (!streamerDeleteSelectionMode) {
        if (streamersList.length > 0) setStreamerDeleteSelectionMode(true);
        return;
    }
    if (selectedStreamerAnalytics.size === 0) return;

    pendingDeleteStreamers = streamersList
        .filter(streamer => selectedStreamerAnalytics.has(streamer.name))
        .map(streamer => streamer.name);
    var displayNames = pendingDeleteStreamers.map(streamer => streamer.replace(".json", ""));
    var displayPreview = displayNames.slice(0, 5).join(', ');
    if (displayNames.length > 5) displayPreview += `, and ${displayNames.length - 5} more`;
    var streamerLabel = displayNames.length === 1
        ? displayNames[0]
        : `${displayNames.length} streamers (${displayPreview})`;
    $('#analytics-delete-modal').removeClass('is-error').addClass('is-active').attr('aria-hidden', 'false');
    $('#analytics-delete-modal .analytics-modal-icon i').attr('class', 'fas fa-trash-alt');
    $('#analytics-delete-modal-title').text('Delete analytics data?');
    $('#analytics-delete-modal-message').text(`Permanently delete all analytics history for ${streamerLabel}?`);
    $('#analytics-delete-modal-note').show();
    $('#analytics-delete-modal-cancel').show();
    $('#analytics-delete-modal-confirm').removeClass('is-loading').addClass('is-danger').text('Delete selected data').prop('disabled', false);
    $('#analytics-delete-modal-cancel').prop('disabled', false).focus();
}

function setStreamerDeleteSelectionMode(enabled) {
    streamerDeleteSelectionMode = enabled;
    selectedStreamerAnalytics.clear();
    lastStreamerCheckboxIndex = null;
    renderStreamers();
}

function handleStreamerAnalyticsSelection(event, index, streamer) {
    var isChecked = event.currentTarget.checked;
    if (event.shiftKey && lastStreamerCheckboxIndex !== null) {
        var start = Math.min(lastStreamerCheckboxIndex, index);
        var end = Math.max(lastStreamerCheckboxIndex, index);
        for (var currentIndex = start; currentIndex <= end; currentIndex++) {
            var currentStreamerName = streamersList[currentIndex].name;
            if (isChecked) selectedStreamerAnalytics.add(currentStreamerName);
            else selectedStreamerAnalytics.delete(currentStreamerName);
            $(`.streamer-delete-checkbox[data-index="${currentIndex}"]`).prop('checked', isChecked);
        }
    } else if (isChecked) {
        selectedStreamerAnalytics.add(streamer);
    } else {
        selectedStreamerAnalytics.delete(streamer);
    }

    lastStreamerCheckboxIndex = index;
    updateStreamerDeleteControls();
}

function updateStreamerDeleteControls() {
    var selectedCount = selectedStreamerAnalytics.size;
    var buttonLabel = streamerDeleteSelectionMode
        ? `Delete selected (${selectedCount})`
        : 'Delete analytics data';
    $('#delete-streamer-analytics .delete-streamer-button-label').text(buttonLabel);
    $('#delete-streamer-analytics')
        .prop('disabled', analyticsDeleteInProgress || streamersList.length === 0 || (streamerDeleteSelectionMode && selectedCount === 0))
        .attr('title', streamerDeleteSelectionMode
            ? 'Permanently delete the selected analytics data'
            : 'Select streamer analytics data to delete');
    $('#cancel-streamer-analytics-selection').toggle(streamerDeleteSelectionMode);
    $('#streamer-selection-hint').toggle(streamerDeleteSelectionMode);
}

function closeAnalyticsDeleteModal() {
    if (analyticsDeleteInProgress) return;

    pendingDeleteStreamers = [];
    $('#analytics-delete-modal').removeClass('is-active is-error').attr('aria-hidden', 'true');
    $('#delete-streamer-analytics').focus();
}

function showAnalyticsDeleteError(message) {
    analyticsDeleteInProgress = false;
    pendingDeleteStreamers = [];
    $('#analytics-delete-modal').addClass('is-error');
    $('#analytics-delete-modal .analytics-modal-icon i').attr('class', 'fas fa-exclamation-triangle');
    $('#analytics-delete-modal-title').text('Could not delete analytics data');
    $('#analytics-delete-modal-message').text(message);
    $('#analytics-delete-modal-note').hide();
    $('#analytics-delete-modal-cancel').hide();
    $('#analytics-delete-modal-confirm').removeClass('is-loading is-danger').text('Close').prop('disabled', false).focus();
    updateStreamerDeleteControls();
}

function confirmStreamerAnalyticsDeletion() {
    if ($('#analytics-delete-modal').hasClass('is-error')) {
        closeAnalyticsDeleteModal();
        return;
    }
    if (pendingDeleteStreamers.length === 0 || analyticsDeleteInProgress) return;

    var streamers = pendingDeleteStreamers.slice();
    analyticsDeleteInProgress = true;
    $('#analytics-delete-modal-cancel').prop('disabled', true);
    $('#analytics-delete-modal-confirm').addClass('is-loading').prop('disabled', true);
    $('#delete-streamer-analytics').prop('disabled', true);
    var deleteRequests = streamers.map(streamer => $.ajax({
        url: `streamers/${encodeURIComponent(streamer)}`,
        method: 'DELETE'
    }));
    Promise.allSettled(deleteRequests).then(function (results) {
        var deletedStreamers = streamers.filter((_streamer, index) => results[index].status === 'fulfilled');
        var failedStreamers = streamers.filter((_streamer, index) => results[index].status === 'rejected');

        if (deletedStreamers.includes(currentStreamer)) {
            if (streamerRefreshTimeout) {
                clearTimeout(streamerRefreshTimeout);
                streamerRefreshTimeout = null;
            }
            streamerDataRequest++;
            localStorage.removeItem("selectedStreamer");
            currentStreamer = null;
        }

        analyticsDeleteInProgress = false;
        $('#analytics-delete-modal-cancel').prop('disabled', false);
        if (failedStreamers.length === 0) {
            closeAnalyticsDeleteModal();
            streamerDeleteSelectionMode = false;
            selectedStreamerAnalytics.clear();
            lastStreamerCheckboxIndex = null;
        } else {
            selectedStreamerAnalytics = new Set(failedStreamers);
            var failureMessage = deletedStreamers.length > 0
                ? `Deleted ${deletedStreamers.length} selection(s), but ${failedStreamers.length} could not be deleted. Please try again.`
                : 'Unable to delete the selected analytics data. Please try again.';
            showAnalyticsDeleteError(failureMessage);
        }
        getStreamers();
    });
}

function sortStreamers() {
    streamersList = streamersList.sort((a, b) => {
        return (a[sortField] > b[sortField] ? 1 : -1) * (sortBy.includes("ascending") ? 1 : -1);
    });
}

function changeSortBy(option) {
    sortBy = option.innerText.trim();
    if (sortBy.includes("Points")) sortField = 'points'
    else if (sortBy.includes("Last activity")) sortField = 'last_activity'
    else sortField = 'name';
    lastStreamerCheckboxIndex = null;
    sortStreamers();
    renderStreamers();
    $('#sorting-by').text(sortBy);
    localStorage.setItem("sort-by", sortBy);
}

function updateAnnotations() {
    if (!chartRendered || $('#points-panel').is(':hidden')) return;

    if ($('#annotations').prop("checked") === true) {
        clearAnnotations()
        if (annotations && annotations.length > 0)
            annotations.forEach((annotation, index) => {
                annotations[index]['id'] = `id-${index}`
                chart.addXaxisAnnotation(annotation, true)
            })
    } else clearAnnotations()
}

function renderPointsChart() {
    if (!chartRendered || $('#points-panel').is(':hidden')) return;

    var series = currentStreamer ? [{
        name: currentStreamer.replace(".json", ""),
        data: pointSeries
    }] : [];

    chart.updateOptions({ title: options.title }, false, false)
        .then(function () {
            return chart.updateSeries(series, true);
        })
        .then(function () {
            updateAnnotations();
        });
}

function clearAnnotations() {
    if (!chartRendered) return;

    if (annotations && annotations.length > 0)
        annotations.forEach((annotation, index) => {
            chart.removeAnnotation(annotation['id'])
        })
    chart.clearAnnotations();
}

function getDropsByCategory() {
    $.getJSON('./drops_by_category', function (response) {
        console.debug('[analytics] Drops response', response);
        renderDropsByCategory(response);
        if (dropsRefreshTimeout) {
            clearTimeout(dropsRefreshTimeout);
        }
        dropsRefreshTimeout = setTimeout(function () {
            getDropsByCategory();
        }, refresh);
    }).fail(function (xhr, status, error) {
        showAnalyticsLoadError(
            `Drops failed to load (${xhr.status || status}): ${error || xhr.responseText || 'Unknown error'}`,
            xhr.responseText
        );
        renderDropsByCategory({ drops: [] });
        if (dropsRefreshTimeout) {
            clearTimeout(dropsRefreshTimeout);
        }
        dropsRefreshTimeout = setTimeout(function () {
            getDropsByCategory();
        }, refresh);
    });
}

function getDropTimestamp(drop) {
    if (drop && drop.x) {
        return drop.x;
    }
    if (drop && drop.datetime) {
        var parsed = Date.parse(drop.datetime);
        if (!isNaN(parsed)) return parsed;
    }
    return 0;
}

function getDropProgressPercent(drop) {
    if (!drop) return 0;

    var progress = Number(drop.percentage_progress);
    if (!isNaN(progress)) {
        return progress;
    }

    var currentMinutes = Number(drop.current_minutes_watched || 0);
    var requiredMinutes = Number(drop.minutes_required || 0);
    if (requiredMinutes > 0) {
        return (currentMinutes / requiredMinutes) * 100;
    }

    return 0;
}

function compareDropsForTable(a, b) {
    var primaryDiff = getDropTimestamp(b) - getDropTimestamp(a);
    if (primaryDiff !== 0) {
        return primaryDiff;
    }

    var progressDiff = getDropProgressPercent(a) - getDropProgressPercent(b);
    if (progressDiff !== 0) {
        return progressDiff;
    }

    return (b.current_minutes_watched || 0) - (a.current_minutes_watched || 0);
}

function getDropEndTimestamp(drop) {
    if (!drop) return -1;

    var rawEnd = drop.drop_end_at || drop.end_at || drop.ends_at || drop.endAt || null;
    if (rawEnd) {
        var parsed = Date.parse(rawEnd);
        if (!isNaN(parsed)) return parsed;

        // Handle legacy values missing timezone marker (treat as UTC).
        if (typeof rawEnd === 'string' && /^[0-9]{4}-[0-9]{2}-[0-9]{2}T/.test(rawEnd) && !rawEnd.endsWith('Z')) {
            parsed = Date.parse(`${rawEnd}Z`);
            if (!isNaN(parsed)) return parsed;
        }
    }
    return -1;
}

function getDropStatusSortPriority(drop, now) {
    var status = String(drop.status || '').toLowerCase();
    var currentMinutes = drop.current_minutes_watched || 0;
    var requiredMinutes = drop.minutes_required || 0;
    var endTimestamp = getDropEndTimestamp(drop);
    var isExpired = endTimestamp !== -1 && endTimestamp < now;
    var isCompleted = requiredMinutes > 0 && currentMinutes >= requiredMinutes;

    if (drop.failed_to_achieve === true || (isExpired && !isCompleted && status !== 'captured')) {
        return 3;
    }
    if (status === 'in_progress' || (!isCompleted && currentMinutes > 0)) {
        return 0;
    }
    if (status === 'captured' || isCompleted) {
        return 2;
    }
    return 1;
}

function getFilteredDropsForCategory(category) {
    var drops = (dropsState.categories[category] || []).slice();
    var now = Date.now();

    if (dropsFilter === 'active') {
        drops = drops.filter((drop) => {
            var endTs = getDropEndTimestamp(drop);
            return endTs === -1 || endTs >= now;
        });
    } else if (dropsFilter === 'last_30' || dropsFilter === 'last_60' || dropsFilter === 'last_90') {
        var days = parseInt(dropsFilter.split('_')[1], 10);
        var cutoff = now - (days * 24 * 60 * 60 * 1000);
        drops = drops.filter((drop) => getDropTimestamp(drop) >= cutoff);
    }

    // Collapse repeated snapshots so each drop appears once using the latest data.
    var dedupedDropsById = {};
    drops.forEach((drop) => {
        var dropKey = [
            drop.drop_id || '',
            drop.item_art_url || '',
            drop.item_name || 'unknown',
            drop.campaign || 'unknown',
            category || 'unknown',
        ].join('|');
        var existing = dedupedDropsById[dropKey];
        if (!existing || getDropTimestamp(drop) > getDropTimestamp(existing)) {
            // Keep a known expiry date when the newer snapshot is missing it.
            if (existing && !drop.drop_end_at && existing.drop_end_at) {
                drop.drop_end_at = existing.drop_end_at;
            }
            dedupedDropsById[dropKey] = drop;
        } else if (existing && !existing.drop_end_at && drop.drop_end_at) {
            existing.drop_end_at = drop.drop_end_at;
        }
    });
    drops = Object.values(dedupedDropsById);

    drops.sort((a, b) => {
        var aEndTimestamp = getDropEndTimestamp(a);
        var bEndTimestamp = getDropEndTimestamp(b);
        var aCampaignClosed = aEndTimestamp !== -1 && aEndTimestamp < now;
        var bCampaignClosed = bEndTimestamp !== -1 && bEndTimestamp < now;
        if (aCampaignClosed !== bCampaignClosed) {
            return aCampaignClosed ? 1 : -1;
        }

        var statusDiff = getDropStatusSortPriority(a, now) - getDropStatusSortPriority(b, now);
        if (statusDiff !== 0) {
            return statusDiff;
        }

        var progressDiff = getDropProgressPercent(b) - getDropProgressPercent(a);
        if (progressDiff !== 0) {
            return progressDiff;
        }

        return getDropTimestamp(b) - getDropTimestamp(a);
    });

    return drops;
}

function getVisibleDropCategories() {
    return dropsState.orderedCategories.filter((category) => {
        return getFilteredDropsForCategory(category).length > 0;
    });
}

function changeDropsFilter(value) {
    dropsFilter = value;
    dropsPage = 1;
    localStorage.setItem('dropsFilter', value);
    renderDropCategoryList();
    renderDropRows();
}

function changeDropsPage(offset) {
    dropsPage += offset;
    renderDropRows();
}

function normalizeDropsData(response) {
    var grouped = (response && response.categories && typeof response.categories === 'object')
        ? response.categories
        : {};

    var drops = (response && Array.isArray(response.drops)) ? response.drops : [];

    if (Object.keys(grouped).length === 0 && drops.length > 0) {
        grouped = {};
        drops.forEach((drop) => {
            var category = drop.category || 'Unknown';
            if (!grouped[category]) grouped[category] = [];
            grouped[category].push(drop);
        });
    }

    Object.keys(grouped).forEach((category) => {
        grouped[category] = grouped[category].slice().sort(compareDropsForTable);
    });

    var orderedCategories = Object.keys(grouped).sort((a, b) => {
        var aLatest = grouped[a][0] ? (grouped[a][0].x || 0) : 0;
        var bLatest = grouped[b][0] ? (grouped[b][0].x || 0) : 0;
        if (bLatest !== aLatest) {
            return bLatest - aLatest;
        }

        var aProgress = grouped[a][0] ? getDropProgressPercent(grouped[a][0]) : 0;
        var bProgress = grouped[b][0] ? getDropProgressPercent(grouped[b][0]) : 0;
        return bProgress - aProgress;
    });

    return {
        categories: grouped,
        orderedCategories: orderedCategories
    };
}

function changeDropCategory(category) {
    currentDropCategory = category;
    dropsPage = 1;
    localStorage.setItem('selectedDropCategory', category);
    renderDropCategoryList();
    renderDropRows();
}

function renderDropCategoryList() {
    var categoriesList = $('#drops-categories-list');
    categoriesList.empty();
    var visibleCategories = getVisibleDropCategories();

    if (visibleCategories.length === 0) {
        currentDropCategory = null;
        categoriesList.append('<li class="is-active"><a href="#">No categories yet</a></li>');
        return;
    }

    if (!currentDropCategory || visibleCategories.indexOf(currentDropCategory) === -1) {
        currentDropCategory = visibleCategories[0];
        dropsPage = 1;
    }

    visibleCategories.forEach((category) => {
        var isActive = currentDropCategory === category;
        var activeClass = isActive ? 'is-active' : '';
        var dropsCount = getFilteredDropsForCategory(category).length;
        var categoryLabel = `${category} (${dropsCount} ${dropsCount === 1 ? 'drop' : 'drops'})`;

        categoriesList.append(`
            <li class="${activeClass}">
                <a href="#" data-category="${escapeHtml(category)}">
                    ${escapeHtml(categoryLabel)}
                </a>
            </li>
        `);
    });

    $('#drops-categories-list a[data-category]').off('click').on('click', function (e) {
        e.preventDefault();
        changeDropCategory($(this).data('category'));
    });
}

function renderDropRows() {
    var dropsItems = $('#drops-items');
    var dropsCategoryTitle = $('#drops-category-title');
    dropsItems.empty();

    if (!currentDropCategory || !dropsState.categories[currentDropCategory]) {
        dropsCategoryTitle.text('');
        dropsItems.append('<div class="drops-empty">No drop events yet.</div>');
        $('#drops-pagination').hide();
        return;
    }

    var drops = getFilteredDropsForCategory(currentDropCategory);
    dropsCategoryTitle.text(`${currentDropCategory} (${drops.length})`);

    if (drops.length === 0) {
        dropsItems.append('<div class="drops-empty">No drops match this filter in this category.</div>');
        $('#drops-pagination').hide();
        return;
    }

    var totalPages = Math.max(1, Math.ceil(drops.length / dropsPerPage));
    if (dropsPage < 1) dropsPage = 1;
    if (dropsPage > totalPages) dropsPage = totalPages;

    var pageStart = (dropsPage - 1) * dropsPerPage;
    var pageDrops = drops.slice(pageStart, pageStart + dropsPerPage);

    $('#drops-page-info').text(`Page ${dropsPage} / ${totalPages}`);
    $('#drops-prev').prop('disabled', dropsPage <= 1);
    $('#drops-next').prop('disabled', dropsPage >= totalPages);
    $('#drops-pagination').toggle(totalPages > 1);

    pageDrops.forEach((drop) => {
        var timestamp = drop.datetime || '';
        var status = String(drop.status || '').toLowerCase();
        var currentMinutes = drop.current_minutes_watched || 0;
        var requiredMinutes = drop.minutes_required || 0;
        var displayedMinutes = Math.min(currentMinutes, requiredMinutes || currentMinutes);
        var progressPercent = Math.max(0, Math.min(100, drop.percentage_progress || 0));
        var progress = `${displayedMinutes}/${requiredMinutes} minutes (${progressPercent}%)`;
        var artUrl = drop.item_art_url || '';
        var itemName = drop.item_name || '';
        var campaign = drop.campaign || '';
        var streamer = drop.streamer || '';
        var dropEndAt = drop.drop_end_at || '';
        var endAtTimestamp = Date.parse(dropEndAt);
        var isExpired = !isNaN(endAtTimestamp) && Date.now() > endAtTimestamp;
        var isCompleted = requiredMinutes > 0 && currentMinutes >= requiredMinutes;
        var isFailed = drop.failed_to_achieve === true || (isExpired && !isCompleted && status !== 'captured');
        var statusClass = status === 'captured' ? 'is-captured' : 'is-progress';
        var expiresAtText = !isNaN(endAtTimestamp) ? new Date(endAtTimestamp).toLocaleString() : '';
        var progressBarClass = statusClass;
        var statusTooltip = '';
        var statusDisplay = status === 'captured'
            ? 'Captured'
            : status === 'in_progress'
                ? 'In Progress'
                : String(status || 'Unknown').replace(/_/g, ' ');

        if (isFailed) {
            statusDisplay = 'Expired';
            statusClass = 'is-failed';
            progressBarClass = 'is-failed';
            statusTooltip = 'Drop expired before completion';
        }

        var metadataSpans = [
            `<span>${escapeHtml(progress)}</span>`,
            campaign ? `<span>${escapeHtml(campaign)}</span>` : '',
            streamer ? `<span>${escapeHtml(streamer)}</span>` : '',
            expiresAtText ? `<span>Expires: ${escapeHtml(expiresAtText)}</span>` : '',
            timestamp ? `<span>${escapeHtml(timestamp)}</span>` : '',
        ].filter(Boolean).join('');

        var failedIcon = isFailed
            ? '<span class="drop-failed-icon">✕</span>'
            : '';
        var statusTitle = statusTooltip
            ? ` title="${escapeHtml(statusTooltip)}"`
            : '';

        var artHtml = artUrl
            ? `<img class="drop-art-thumb" src="${escapeHtml(artUrl)}" alt="${escapeHtml(itemName)} art">`
            : '<div class="drop-art-placeholder"><i class="fa fa-gift" aria-hidden="true"></i></div>';

        dropsItems.append(`
            <div class="drop-row">
                <div class="drop-row-art">${artHtml}</div>
                <div class="drop-row-body">
                    <div class="drop-row-top">
                        <div class="drop-item-name">${failedIcon}${escapeHtml(itemName || 'Unknown Drop')}</div>
                        <span class="drop-status ${statusClass}"${statusTitle}>${escapeHtml(statusDisplay)}</span>
                    </div>
                    <div class="drop-progress-block">
                        <div class="drop-progress-bar" aria-hidden="true">
                            <div class="drop-progress-fill ${progressBarClass}" style="width: ${progressPercent}%;"></div>
                        </div>
                        <div class="drop-progress-label">${escapeHtml(progress)}</div>
                    </div>
                    <div class="drop-row-meta">
                        ${metadataSpans}
                    </div>
                </div>
            </div>
        `);
    });
}

function renderDropsByCategory(response) {
    dropsState = normalizeDropsData(response);

    var savedDropCategory = localStorage.getItem('selectedDropCategory');
    if (savedDropCategory && dropsState.categories[savedDropCategory]) {
        currentDropCategory = savedDropCategory;
    }

    if (!currentDropCategory || !dropsState.categories[currentDropCategory]) {
        currentDropCategory = dropsState.orderedCategories.length > 0
            ? dropsState.orderedCategories[0]
            : null;
    }

    renderDropCategoryList();
    renderDropRows();
}

function escapeHtml(text) {
    return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// Toggle
$('#dark-mode').click(() => {
    toggleDarkMode();
});

$('.dropdown').click(() => {
    $('.dropdown').toggleClass('is-active');
});

// Input date
$('#startDate').change(() => {
    startDate = new Date(`${$('#startDate').val()}T00:00:00`);
    getStreamerData(currentStreamer);
});
$('#endDate').change(() => {
    endDate = new Date(`${$('#endDate').val()}T00:00:00`);
    getStreamerData(currentStreamer);
});
