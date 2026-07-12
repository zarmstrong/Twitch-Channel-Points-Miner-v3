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
            datetimeUTC: false
        }
    },
    tooltip: {
        theme: 'dark',
        shared: false,
        x: {
            show: true,
            format: 'HH:mm:ss dd MMM',
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
var currentStreamer = null;
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

function switchDashboardTab(tabName) {
    var isPoints = tabName !== 'drops';
    $('#points-panel').toggle(isPoints);
    $('#drops-panel').toggle(!isPoints);

    $('#tab-points').toggleClass('is-link', isPoints);
    $('#tab-drops').toggleClass('is-link', !isPoints);

    localStorage.setItem('dashboardTab', isPoints ? 'points' : 'drops');
}

var startDate = new Date();
startDate.setDate(startDate.getDate() - daysAgo);
var endDate = new Date();

$(document).ready(function () {
    var savedDashboardTab = localStorage.getItem('dashboardTab') || 'points';
    switchDashboardTab(savedDashboardTab);
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
                $("#log-content").append(data);
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

    chart.render();

    if (!localStorage.getItem("annotations")) localStorage.setItem("annotations", true);
    if (!localStorage.getItem("dark-mode")) localStorage.setItem("dark-mode", true);
    if (!localStorage.getItem("sort-by")) localStorage.setItem("sort-by", "Name ascending");

    // Restore settings from localStorage on page load
    $('#annotations').prop("checked", localStorage.getItem("annotations") === "true");
    $('#dark-mode').prop("checked", localStorage.getItem("dark-mode") === "true");

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

function changeStreamer(streamer, index) {
    $("li").removeClass("is-active")
    $("li").eq(index - 1).addClass('is-active');
    currentStreamer = streamer;

    // Update the chart title with the current streamer's name
    options.title.text = `${streamer.replace(".json", "")}'s channel points (dates are displayed in UTC)`;
    chart.updateOptions(options);

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

            chart.updateSeries([{
                name: streamer.replace(".json", ""),
                data: response["series"]
            }], true)
            clearAnnotations();
            annotations = response["annotations"];
            updateAnnotations();
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
        streamersList = response;
        sortStreamers();

        // Restore the selected streamer from localStorage on page load
        var selectedStreamer = localStorage.getItem("selectedStreamer");

        if (selectedStreamer) {
            currentStreamer = selectedStreamer;
        } else {
            // If no selected streamer is found, default to the first streamer in the list
            currentStreamer = streamersList.length > 0 ? streamersList[0].name : null;
        }

        // Ensure the selected streamer is still active and scrolled into view
        renderStreamers();
    });
}

function renderStreamers() {
    $("#streamers-list").empty();
    var promised = new Promise((resolve, reject) => {
        streamersList.forEach((streamer, index, array) => {
            displayname = streamer.name.replace(".json", "");
            if (sortField == 'points') displayname = "<font size='-2'>" + streamer['points'] + "</font>&nbsp;" + displayname;
            else if (sortField == 'last_activity') displayname = "<font size='-2'>" + formatDate(streamer['last_activity']) + "</font>&nbsp;" + displayname;
            var isActive = currentStreamer === streamer.name;
            if (!isActive && localStorage.getItem("selectedStreamer") === null && index === 0) {
                isActive = true;
                currentStreamer = streamer.name;
            }
            var activeClass = isActive ? 'is-active' : '';
            var listItem = `<li id="streamer-${streamer.name}" class="${activeClass}"><a onClick="changeStreamer('${streamer.name}', ${index + 1}); return false;">${displayname}</a></li>`;
            $("#streamers-list").append(listItem);
            if (isActive) {
                // Scroll the selected streamer into view
                document.getElementById(`streamer-${streamer.name}`).scrollIntoView({
                    behavior: 'smooth',
                    block: 'center'
                });
            }
            if (index === array.length - 1) resolve();
        });
    });
    promised.then(() => {
        changeStreamer(currentStreamer, streamersList.findIndex(streamer => streamer.name === currentStreamer) + 1);
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
    sortStreamers();
    renderStreamers();
    $('#sorting-by').text(sortBy);
    localStorage.setItem("sort-by", sortBy);
}

function updateAnnotations() {
    if ($('#annotations').prop("checked") === true) {
        clearAnnotations()
        if (annotations && annotations.length > 0)
            annotations.forEach((annotation, index) => {
                annotations[index]['id'] = `id-${index}`
                chart.addXaxisAnnotation(annotation, true)
            })
    } else clearAnnotations()
}

function clearAnnotations() {
    if (annotations && annotations.length > 0)
        annotations.forEach((annotation, index) => {
            chart.removeAnnotation(annotation['id'])
        })
    chart.clearAnnotations();
}

function getDropsByCategory() {
    $.getJSON('./drops_by_category', function (response) {
        renderDropsByCategory(response);
        if (dropsRefreshTimeout) {
            clearTimeout(dropsRefreshTimeout);
        }
        dropsRefreshTimeout = setTimeout(function () {
            getDropsByCategory();
        }, refresh);
    }).fail(function () {
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
        var status = drop.status || '';
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
        var statusDisplay = String(status || 'unknown').replace(/_/g, ' ');

        if (isFailed) {
            status = 'EXPIRED - Failed to achieve';
            statusClass = 'is-failed';
            progressBarClass = 'is-failed';
        } else if (status === 'in_progress') {
            statusDisplay = 'in progress';
        }

        var metadataSpans = [
            `<span>${escapeHtml(progress)}</span>`,
            campaign ? `<span>${escapeHtml(campaign)}</span>` : '',
            streamer ? `<span>${escapeHtml(streamer)}</span>` : '',
            expiresAtText ? `<span>Expires: ${escapeHtml(expiresAtText)}</span>` : '',
            timestamp ? `<span>${escapeHtml(timestamp)}</span>` : '',
        ].filter(Boolean).join('');

        var failedIcon = isFailed
            ? '<span class="drop-failed-icon" title="Failed to achieve">✕</span>'
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
                        <span class="drop-status ${statusClass}">${escapeHtml(statusDisplay)}</span>
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
$('#annotations').click(() => {
    updateAnnotations();
});
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
