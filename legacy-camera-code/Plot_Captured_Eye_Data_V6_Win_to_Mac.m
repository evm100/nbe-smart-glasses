

function Plot_Captured_Eye_Data_V6_Win_to_Mac(folder)
% =========================================================================
% PLOT_CAPTURED_EYE_DATA_V6_WIN_TO_MAC
% Review, TRIM and STITCH sessions recorded with Simplified_eye_V6_win_to_mac.m
% (one folder per session in ~/Desktop/MyDataFolder, each holding a final
% session_*.mat). Reads the final .mat only -- no autosave/crash recovery.
%
% Usage:
%   >> Plot_Captured_Eye_Data_V6_Win_to_Mac              % folder picker
%   >> Plot_Captured_Eye_Data_V6_Win_to_Mac(folder)      % one session folder
%   >> Plot_Captured_Eye_Data_V6_Win_to_Mac(fullfile(<MyDataFolder>, 'FrankensteinData'))
%   >> Plot_Captured_Eye_Data_V6_Win_to_Mac('test')      % offline self-check
%
% WINDOWS OPENED FOR A SESSION
%   1. Eye Data      motion, centroid, per-minute bars, summary, velocity
%   2. Sensors       PPG | derivative + beats, heart rate, temperature | light
%   3. Per-Minute    the live session's trend panels
%   4. TRIM          motion + amplified centroid, the ONLY window you cut in,
%                    and where the two blink THRESHOLDS are adjusted
%   All x-axes are the real CLOCK TIME of the recording (from the wall_time
%   column the recorder writes on every row).
%
% TRIMMING (window 4)
%   Drag across a bad section on either plot -> shaded red. Repeat as needed.
%   Undo / Clear / Show all, then "Save trimmed data". Every stream (frames,
%   blinks, heart, beats, BPM, temperature, light) is cut at the SAME clock
%   times. Zoom with the axes toolbar (top-right of a plot); switch zoom off
%   again to make selections.
%
%   NOTHING ORIGINAL IS EVER MODIFIED. Saving writes new files next to it:
%     trimmed_<stream>.csv   wall_time, time_s, data  (one row per sample)
%     trimmed_cuts.csv       the cut list (clock + seconds)
%   The trim window always starts from the ORIGINAL data with your previous
%   cuts re-loaded, so a cut can be undone later and nothing is cut twice.
%   The analysis windows show the trimmed version whenever it exists.
%
% STITCHING
%   Every save also rebuilds <MyDataFolder>/FrankensteinData/ from ALL
%   sessions that have been trimmed (a session with nothing to cut: open it
%   and press Save with no cuts). Same file layout, one continuous clock;
%   sources.csv lists what went in. Plot it by passing that folder.
%
% PAUSES (recorder PAUSE button)
%   Every time-axis plot (and the trim window) shades each pause grey and
%   labels it "PAUSE n  hh:mm  (duration)". Paused minutes have no per-minute
%   values. Pauses travel through trimming and FrankensteinData.
%
% STIMULI (recorder MARK button)
%   Every time-axis plot shows a pink line labelled with the stimulus name
%   (e.g. "hand on ice") at the moment MARK was pressed. Stimuli are never
%   cut by trimming; they are saved as trimmed_stimuli.csv and stitched into
%   FrankensteinData like everything else.
%
% BLINK THRESHOLDS (TRIM window)
%   Each plot has a DASHED copy of its threshold line and a vertical slider on
%   its right. The slider moves only the dashed line; "Compute" applies it:
%   blinks are re-detected from the logged signals with the live recorder's
%   own rules (hysteresis, re-open at half the closed threshold, 0.25 s /
%   0.2 s refractory), the solid line moves there, and every window updates.
%   Closed threshold (centroid): baseline + N px, recorded with 18.
%   Motion threshold: recorded with 0.18. At the recorded values the
%   recorded blinks are used unchanged. Saved with the trim in
%   analysis_settings.csv. Not reproducible offline: the live head-movement
%   suppression (its inputs were not logged), so re-detected counts can be
%   slightly higher than the live ones at the same threshold.
% =========================================================================

    if nargin > 0 && strcmp(folder, 'test'), selfTest(); return; end

    if nargin < 1 || isempty(folder)
        desk = desktopPath();
        defaultRoot = fullfile(desk, 'MyDataFolder');
        if ~isfolder(defaultRoot), defaultRoot = desk; end
        if isempty(defaultRoot) || ~isfolder(defaultRoot), defaultRoot = pwd; end
        folder = uigetdir(defaultRoot, 'Select a session folder (or FrankensteinData)');
        if isequal(folder, 0), fprintf('Cancelled.\n'); return; end
    end
    fprintf('Folder: %s\n', folder);

    if isFrankenstein(folder)
        fprintf('FrankensteinData: stitched trimmed sessions.\n');
        plotAll(loadTrimmed(folder), folder);
        return;
    end

    % Selected MyDataFolder itself: open the newest session in it.
    if isempty(sessionFiles(folder))
        sub = dir(fullfile(folder, 'session_*'));
        sub = sub([sub.isdir]);
        if ~isempty(sub)
            [~, k] = max([sub.datenum]);
            folder = fullfile(folder, sub(k).name);
            fprintf('Opening most recent session: %s\n', sub(k).name);
        end
    end

    Dorig = loadOriginal(folder);
    if isempty(Dorig), return; end

    if hasTrimmed(folder)
        fprintf('Showing TRIMMED data (originals untouched; trim window shows the originals).\n');
        plotAll(loadTrimmed(folder), folder);
    else
        plotAll(Dorig, folder);
    end
    trimWindow(Dorig, folder);
end

% =========================================================================
% LOADING
% =========================================================================
function D = loadOriginal(folder)
    % The session's final .mat (newest, if a manual Space-save left several).
    matFiles = sessionFiles(folder);
    if isempty(matFiles)
        errordlg(sprintf('No session_*.mat found in:\n%s', folder), 'No data');
        D = [];
        return;
    end
    [~, idx] = max([matFiles.datenum]);
    fprintf('Loading: %s\n', matFiles(idx).name);
    D = load(fullfile(matFiles(idx).folder, matFiles(idx).name));
    D.t0 = sessionStart(folder, D);
    D.trimmed = false;
    D = normalize(D, folder);
    % Keep the blinks exactly as recorded so thresholds can always go back.
    D.rec = struct('blinkTimestamps', D.blinkTimestamps, 'centBlinkTimestamps', D.centBlinkTimestamps, ...
                   'centBlinkDur', D.centBlinkDur, 'blinkDurations', D.blinkDurations, ...
                   'blinkCount', D.blinkCount, 'centBlinkCount', D.centBlinkCount);
    [cd, mt] = readSettings(folder);
    D = applyThresholds(D, cd, mt);
end
function D = loadTrimmed(folder)
    % trimmed_*.csv from a session folder or from FrankensteinData. Per-minute
    % values are recomputed from what is left, since cuts change every bin.
    D = struct();
    S = streamDefs();
    for i = 1:size(S, 1)
        D = readStream(D, fullfile(folder, ['trimmed_' S{i, 1} '.csv']), [S(i, 3), S{i, 4}]);
    end
    D.t0 = NaT;
    for i = 1:size(S, 1)
        D.t0 = firstRowStart(fullfile(folder, ['trimmed_' S{i, 1} '.csv']));
        if ~isnat(D.t0), break; end
    end
    if isnat(D.t0), D.t0 = sessionStart(folder, D); end
    [D.stimTimes, D.stimNames] = readStimuli(fullfile(folder, 'trimmed_stimuli.csv'));
    [~, D.sessionTag] = fileparts(folder);
    D.trimmed = true;
    D.blinkCount     = numel(D.blinkTimestamps);
    D.centBlinkCount = numel(D.centBlinkTimestamps);
    D.blinkDurations = D.centBlinkDur(~isnan(D.centBlinkDur));
    % Blinks in trimmed files were detected with the saved thresholds.
    % ponytail: FrankensteinData has no settings file, so its threshold lines
    % show the defaults even if some sessions were saved with others.
    [D.closedDelta, D.motionThr] = readSettings(folder);
    D = minuteBins(D);
    D = normalize(D, folder);
end

function D = normalize(D, folder)
    % One shape for every source (final .mat, trimmed, stitched),
    % so the plotting code never has to ask where the data came from.
    isV2 = isfield(D, 'centYlog') || isfield(D, 'centYampLog');
    if isV2
        if ~isfield(D, 'centYlog'),    D.centYlog    = []; end
        if ~isfield(D, 'centYampLog'), D.centYampLog = D.centYlog; end
    else
        if ~isfield(D, 'lidYlog'), D.lidYlog = []; end
    end
    D.isV2 = isV2;
    if ~isfield(D, 'stimTimes'), D.stimTimes = []; end
    if ~isfield(D, 'stimNames'), D.stimNames = {}; end
    D.stimTimes = D.stimTimes(:).'; D.stimNames = reshape(cellstr(D.stimNames), 1, []);
    if isfield(D, 'pauseLog') && ~isempty(D.pauseLog) && ~isfield(D, 'pauseStart')
        D.pauseStart = D.pauseLog(:, 1).'; D.pauseDur = diff(D.pauseLog, 1, 2).';
    end
    need = {'timeLog', 'diffLog', 'baseYlog', 'blinkTimestamps', 'blinkDurations', 'pauseStart', 'pauseDur', ...
            'minBinCounts', 'minBinAvgDur', 'minBinMaxDur', ...
            'centBlinkTimestamps', 'minBinCentCounts', ...
            'heartTimeLog', 'heartFiltLog', 'heartDerivLog', 'beatTimes', ...
            'bpmTimeLog', 'bpmValLog', 'tempTimeLog', 'tempAmbLog', 'tempObjLog', ...
            'ldrTimeLog', 'ldrValLog', 'ldrStateLog', 'minBinBpm', 'minBinTempObj', 'minBinTempAmb'};
    for k = 1:numel(need)
        if ~isfield(D, need{k}), D.(need{k}) = []; end
        D.(need{k}) = D.(need{k})(:).';
    end
    if ~isfield(D, 'centBlinkCount'), D.centBlinkCount = numel(D.centBlinkTimestamps); end
    if ~isfield(D, 'blinkCount'),     D.blinkCount     = numel(D.blinkTimestamps); end
    if ~isfield(D, 'sessionTag'),     [~, D.sessionTag] = fileparts(folder); end
    if ~isfield(D, 'centBlinkDur') || isempty(D.centBlinkDur)
        % The recorder keeps durations as a plain list, one per centroid
        % closure that re-opened. Pair them with closure onsets in order.
        % ponytail: a closure whose duration was out of range (<0.03 or >30 s)
        % shifts later pairings by one; rare, and only affects durations.
        D.centBlinkDur = nan(size(D.centBlinkTimestamps));
        n = min(numel(D.centBlinkTimestamps), numel(D.blinkDurations));
        D.centBlinkDur(1:n) = D.blinkDurations(1:n);
    end
    D.centBlinkDur = D.centBlinkDur(:).';
    nT = numel(D.timeLog);
    if ~isfield(D, 'centXlog'),    D.centXlog    = []; end
    if ~isfield(D, 'suppressLog'), D.suppressLog = []; end
    D.centXlog    = padtrim(D.centXlog, nT);
    D.suppressLog = padtrim(double(D.suppressLog), nT);   % NaN = not logged (older sessions)
    if ~isfield(D, 'pupilXlog'), D.pupilXlog = []; D.pupilYlog = []; end
    D.pupilXlog = padtrim(D.pupilXlog, nT);                % NaN = no usable pupil / older session
    D.pupilYlog = padtrim(D.pupilYlog, nT);
    D.diffLog  = padtrim(D.diffLog,  nT);
    D.baseYlog = padtrim(D.baseYlog, nT);
    if isV2
        D.centYlog    = padtrim(D.centYlog,    nT);
        D.centYampLog = padtrim(D.centYampLog, nT);
    else
        D.lidYlog = padtrim(D.lidYlog(:).', nT);
    end
    if ~isfield(D, 'closedDelta'), D.closedDelta = 18;   end
    if ~isfield(D, 'motionThr'),   D.motionThr   = 0.18; end
    if ~isfield(D, 'note'),        D.note        = '';   end
    D.recMin = sum(coverageSec(D.timeLog)) / 60;    % minutes actually recorded
    % Recorded per-minute bins count a paused minute as "0 blinks"; rebuild
    % from coverage so it is "no data" instead.
    if (isempty(D.minBinCounts) || (~isempty(D.pauseStart) && ~D.trimmed)) && nT > 0
        D = minuteBins(D);
    end
    D = gazeBins(D);
end

function D = gazeBins(D)
    % Gaze shifts / fixations / movement, whole session and per minute, from
    % the per-frame pupil log with the recorder's own rules (GazeMetrics).
    % Sessions recorded before the pupil finder fall back to the dark-pixel
    % centroid with closing frames and head moves left out.
    t = D.timeLog; x = D.pupilXlog; y = D.pupilYlog;
    D.gazeSource = 'pupil';
    if all(isnan(x)) && D.isV2 && any(~isnan(D.centYlog))
        x = D.centXlog; y = D.centYlog;
        if all(isnan(x)), x = 0 * y; end                   % before centX was logged: vertical only
        bad = (D.centYampLog - D.baseYlog) > D.closedDelta / 2 | D.suppressLog == 1;
        x(bad) = NaN; y(bad) = NaN;
        D.gazeSource = 'older session: dark-pixel centroid';
    end
    D.gaze = GazeMetrics(t, x, y);
    nMin = max(1, ceil(max([t, 0]) / 60));
    [D.minBinGazeShifts, D.minBinFixDur, D.minBinGazeMove] = deal(nan(1, nMin));
    for m = 1:nMin
        k = t >= (m - 1) * 60 & t < m * 60;
        Gm = GazeMetrics(t(k), x(k), y(k));
        D.minBinGazeShifts(m) = Gm.rate; D.minBinFixDur(m) = Gm.fix; D.minBinGazeMove(m) = Gm.moveMean;
    end
end

function t0 = sessionStart(folder, D)
    % Clock time of time_s = 0. Best source first.
    t0 = NaT;
    if isfield(D, 'sessionStartWall') && isdatetime(D.sessionStartWall)
        t0 = D.sessionStartWall;
    end
    if isnat(t0)
        % Sessions recorded before sessionStartWall was saved: the folder /
        % tag name, which is late by up to the sync wait (<= 30 s).
        tok = regexp([folder ' ' char(string(getfieldOr(D, 'sessionTag')))], ...
                     '(\d{4}-\d{2}-\d{2}_\d{6})', 'tokens', 'once');
        if ~isempty(tok), t0 = datetime(tok{1}, 'InputFormat', 'yyyy-MM-dd_HHmmss'); end
    end
    if isnat(t0)
        info = dir(folder);
        t0 = datetime(info(1).datenum, 'ConvertFrom', 'datenum');
        warning('No recording clock found -- using the folder date for %s.', folder);
    end
    t0.TimeZone = '';
    t0.Format = 'yyyy-MM-dd HH:mm:ss.SSS';
end

function v = getfieldOr(D, f)
    if isfield(D, f), v = D.(f); else, v = ''; end
end

function t0 = firstRowStart(file)
    % wall_time - time_s of the first data row = clock time of time_s 0.
    t0 = NaT;
    if ~isfile(file), return; end
    fid = fopen(file, 'r');
    hdr = fgetl(fid); line = fgetl(fid);
    fclose(fid);
    if ~ischar(hdr) || ~startsWith(hdr, 'wall_time') || ~ischar(line), return; end
    p = strsplit(line, ',');
    if numel(p) < 2, return; end
    w = datetime(p{1}, 'InputFormat', 'yyyy-MM-dd HH:mm:ss.SSS');
    t0 = w - seconds(str2double(p{2}));
end

% =========================================================================
% BLINK THRESHOLDS -- offline re-detection with the live recorder's rules
% =========================================================================
function [cd, mt] = readSettings(folder)
    cd = 18; mt = 0.18;                     % the values every session was recorded with
    f = fullfile(folder, 'analysis_settings.csv');
    if ~isfile(f), return; end
    M = readmatrix(f, 'NumHeaderLines', 1);
    if numel(M) >= 2, cd = M(1); mt = M(2); end
end

function D = applyThresholds(D, cd, mt)
    % Always starts from the RECORDED blinks (D.rec), so moving a slider back
    % to 18 / 0.18 gives exactly what the recorder counted.
    D.closedDelta = cd; D.motionThr = mt;
    if ~isfield(D, 'rec'), return; end
    f = fieldnames(D.rec);
    for k = 1:numel(f), D.(f{k}) = D.rec.(f{k}); end
    changed = false;
    if abs(mt - 0.18) > 1e-9 && ~isempty(D.timeLog)
        D.blinkTimestamps = detectMotion(D.timeLog, D.diffLog, mt, D.suppressLog == 1);
        D.blinkCount = numel(D.blinkTimestamps);
        changed = true;
    end
    if abs(cd - 18) > 1e-9 && ~isempty(D.timeLog) && isfield(D, 'centYampLog')
        [D.centBlinkTimestamps, D.centBlinkDur] = detectCentroid(D.timeLog, D.centYampLog, D.baseYlog, cd, D.suppressLog == 1);
        D.centBlinkCount = numel(D.centBlinkTimestamps);
        D.blinkDurations = D.centBlinkDur(~isnan(D.centBlinkDur));
        changed = true;
    end
    if changed || isfield(D, 'binsChanged')
        D = minuteBins(D);
        D.binsChanged = true;   % recorded bins no longer match; keep recomputing
    end
end

function tB = detectMotion(t, sm, thr, sup)
    % = Simplified_eye_V6 MOTION PEAK block, run over the logged smoothed
    % frame difference, measured from the resting MOTION FLOOR like the live
    % loop (dim light lifts the floor above the old fixed release level):
    % fires at floor + (thr - 0.05), counts when it falls back 45% of the way
    % up, unless the head-move filter was on at that frame (sup, logged since
    % 2026-10-28; older sessions: no filter offline). A peak > 1.5 s is noise.
    if nargin < 4 || isempty(sup), sup = false(size(t)); end
    minGap = 0.2; inPeak = false; last = 0; floorM = 0.05; pStart = 0;
    tB = zeros(1, 0);
    for i = 1:numel(t)
        since = t(i) - last;
        effThr = max(thr, floorM + (thr - 0.05)); release = floorM + 0.45 * (effThr - floorM);
        if ~inPeak
            floorM = 0.98 * floorM + 0.02 * min(sm(i), effThr);
        elseif t(i) - pStart > 1.5
            inPeak = false; floorM = 0.9 * floorM + 0.1 * sm(i);
        end
        if ~inPeak && sm(i) > effThr && since > minGap
            inPeak = true; pStart = t(i);
        elseif inPeak && sm(i) < release
            inPeak = false;
            if since > minGap && ~sup(i), tB(end+1) = t(i); last = t(i); end %#ok<AGROW>
        end
    end
end

function [tOn, dur] = detectCentroid(t, amp, base, cd, sup)
    % = Simplified_eye_V6 CENTROID CLOSURE DETECTOR over the logged amplified
    % centroid and baseline: close when amp - base > cd, re-open when the
    % 3-frame median falls below cd/2 (the live 18/9 ratio) OR has stayed
    % under cd for 0.3 s (live stuck-marker rule; that closure ends where it
    % first went under). Not reproducible offline: the live "pupil visible
    % -> open" rule, whose per-frame input is not logged.
    if nargin < 5 || isempty(sup), sup = false(size(t)); end
    openMargin = cd / 2; minGap = 0.25; reopenHold = 0.3;
    dev = amp - base;
    devMed = movmedian(amp, [2 0], 'omitnan') - base;   % causal, like the live ring buffer
    closed = false; last = -Inf; onT = NaN; below = NaN;
    tOn = zeros(1, 0); dur = zeros(1, 0);
    for i = 1:numel(t)
        if closed && i > 1 && t(i) - t(i-1) > 1        % data gap / cut: duration unknown
            closed = false; dur(end) = NaN;
        end
        if closed && devMed(i) < cd, if isnan(below), below = t(i); end, else, below = NaN; end
        % live: head movement blocks only the START of a closure
        if ~sup(i) && ~closed && dev(i) > cd && t(i) - last > minGap
            closed = true; onT = t(i); last = t(i);
            tOn(end+1) = t(i); dur(end+1) = NaN; %#ok<AGROW>
        elseif closed && (devMed(i) < openMargin || ...
                (devMed(i) < cd && ~isnan(below) && t(i) - below >= reopenHold))
            closed = false;
            e = t(i); if devMed(i) >= openMargin, e = below; end
            below = NaN;
            d = e - onT;
            if d >= 0.03 && d <= 30, dur(end) = d; end
        end
    end
end

function D = applyCutsD(D, cuts)
    % Every stream cut at the same clock times; per-minute values rebuilt.
    S = streamDefs();
    for i = 1:size(S, 1)
        M = streamMatrix(D, S(i, :));
        M = M(~inCuts(M(:, 1), cuts), :);
        f = [S(i, 3), S{i, 4}];
        for j = 1:numel(f), D.(f{j}) = M(:, j).'; end
    end
    D.blinkCount     = numel(D.blinkTimestamps);
    D.centBlinkCount = numel(D.centBlinkTimestamps);
    D.blinkDurations = D.centBlinkDur(~isnan(D.centBlinkDur));
    if ~isempty(cuts), D.trimmed = true; end
    D = minuteBins(D);
    D = gazeBins(D);
    D.recMin = sum(coverageSec(D.timeLog)) / 60;
end

% =========================================================================
% STREAMS -- the single list every load / cut / write / stitch goes through
% =========================================================================
function S = streamDefs()
    % name (file suffix) | CSV header after wall_time | time field | data fields
    S = {
        'frames',          'time_s,motion,centY,centYamp,baseY,centX,head_move,pupilX,pupilY', 'timeLog', {'diffLog', 'centYlog', 'centYampLog', 'baseYlog', 'centXlog', 'suppressLog', 'pupilXlog', 'pupilYlog'}
        'heart',           'time_s,filtered,derivative',         'heartTimeLog', {'heartFiltLog', 'heartDerivLog'}
        'ldr',             'time_s,ldr,light_state',             'ldrTimeLog',   {'ldrValLog', 'ldrStateLog'}
        'temp',            'time_s,ambient_C,object_C',          'tempTimeLog',  {'tempAmbLog', 'tempObjLog'}
        'bpm',             'time_s,bpm',                         'bpmTimeLog',   {'bpmValLog'}
        'beats',           'time_s',                             'beatTimes',    {}
        'blinks_motion',   'time_s',                             'blinkTimestamps', {}
        'blinks_centroid', 'time_s,duration_s',                  'centBlinkTimestamps', {'centBlinkDur'}
        'pauses',          'time_s,duration_s',                  'pauseStart',   {'pauseDur'}
        };
end

function M = streamMatrix(D, row)
    % [time, data...] as an n x k matrix, columns padded to the time length.
    t = D.(row{3})(:);
    M = t;
    for j = 1:numel(row{4})
        f = row{4}{j};
        if isfield(D, f), v = padtrim(D.(f), numel(t)); else, v = nan(1, numel(t)); end
        M = [M, v(:)]; %#ok<AGROW>
    end
end

% =========================================================================
% TRIMMING
% =========================================================================
function trimWindow(D, folder)
    [~, name] = fileparts(folder);
    cuts = readCuts(folder);
    regions = cell(0, 1);
    drag = [];
    cd = D.closedDelta; mt = D.motionThr;

    figT = figure('Name', ['TRIM: ' name], 'NumberTitle', 'off', ...
        'Color', [0.08 0.08 0.08], 'Position', [120 100 1450 800]);
    % Plots stop at x = 0.88 to leave room for the threshold slider beside each.
    posA1 = [0.06 0.57 0.82 0.31]; posA2 = [0.06 0.11 0.82 0.31];
    ax1 = axes(figT, 'Position', posA1);
    ax2 = axes(figT, 'Position', posA2);
    darkAx(ax1); darkAx(ax2);

    if isempty(D.timeLog)
        noData(ax1, 'No frame data in this session -- nothing to trim against');
        return;
    end
    % Full resolution here (no decimation): this is the window you zoom into
    % to place cuts and judge single blinks.
    [tb, Y] = breakGaps(D.timeLog, [D.diffLog; D.centYampLog; D.baseYlog], 1.0);
    tc = D.t0 + seconds(tb);
    plot(ax1, tc, Y(1, :), 'm-', 'LineWidth', 0.6, 'DisplayName', 'Motion signal');
    hMotThr = yline(ax1, mt, '-', 'Color', [1 0.25 0.25], 'LineWidth', 1.4, 'DisplayName', 'Motion threshold (applied)');
    % The "virtual" line: same colour, dashed, starts on top of the real one.
    % Only the slider moves it; nothing is recounted until Compute.
    hMotV = yline(ax1, mt, '--', 'Color', [1 0.25 0.25], 'LineWidth', 1.8, 'DisplayName', 'New threshold (slider)');
    hMotTicks = plot(ax1, NaT, NaN, 'g|', 'MarkerSize', 7, 'DisplayName', 'Motion blink');
    % Fitted to the signal (it rarely passes 0.4), not 0-1.2: the thresholds
    % live down there and need the vertical room to be placed precisely.
    sorted = sort(D.diffLog(~isnan(D.diffLog)));
    top = max(0.4, 1.25 * sorted(max(1, ceil(0.999 * numel(sorted)))));
    ylim(ax1, [-0.02 top]);
    motTickY = 0.94 * top;
    ylabel(ax1, 'Frame Diff (norm.)', 'Color', 'w');
    title(ax1, 'Motion Signal', 'Color', 'w', 'FontWeight', 'bold');
    leg(ax1); markEvents(ax1, D);

    plot(ax2, tc, Y(2, :), 'g-', 'LineWidth', 0.6, 'DisplayName', 'Amplified centroid Y');
    plot(ax2, tc, Y(3, :), 'y-', 'LineWidth', 1.0, 'DisplayName', 'Rolling baseline');
    hClosed = plot(ax2, tc, Y(3, :) + cd, '-', 'Color', [1 0.45 0.1], 'LineWidth', 1.2, ...
                   'DisplayName', 'Closed threshold (applied)');
    hClosedV = plot(ax2, tc, Y(3, :) + cd, '--', 'Color', [1 0.45 0.1], 'LineWidth', 1.6, ...
                   'DisplayName', 'New threshold (slider)');
    hCentTicks = plot(ax2, NaT, NaN, 'c|', 'MarkerSize', 7, 'DisplayName', 'Centroid blink');
    set(ax2, 'YDir', 'reverse'); ylim(ax2, [0 80]);
    ylabel(ax2, 'Centroid Y (px, 0 = top)', 'Color', 'w');
    xlabel(ax2, 'Clock time', 'Color', 'w');
    title(ax2, 'Amplified Centroid vs Baseline  (down = closing)', 'Color', 'w', 'FontWeight', 'bold');
    leg(ax2); markEvents(ax2, D);

    linkaxes([ax1 ax2], 'x');
    xFull = [min(tc), max(tc)];
    xlim(ax1, xFull);
    % Drag must SELECT, not pan. Zoom/pan stay available on the axes toolbar,
    % horizontally only: the motion slider is laid out against the plot's
    % fixed y-range, so a vertical zoom would put its knob beside the wrong value.
    disableDefaultInteractivity(ax1); disableDefaultInteractivity(ax2);
    set(zoom(figT), 'Motion', 'horizontal'); set(pan(figT), 'Motion', 'horizontal');

    uicontrol(figT, 'Style', 'text', 'Units', 'normalized', 'Position', [0.01 0.945 0.55 0.045], ...
        'BackgroundColor', [0.08 0.08 0.08], 'ForegroundColor', [0.85 0.85 0.85], ...
        'HorizontalAlignment', 'left', 'FontSize', 10, 'String', ...
        {'Drag across a bad section on either plot to cut it.  Zoom: axes toolbar (turn it off to select).', ...
         'Originals are never changed -- Save writes trimmed_*.csv and updates FrankensteinData.'});
    btn = @(pos, str, cb) uicontrol(figT, 'Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', pos, 'String', str, 'FontSize', 11, 'Callback', cb);
    btn([0.57 0.95 0.08 0.04], 'Undo cut', @(~, ~) undoCut());
    btn([0.655 0.95 0.08 0.04], 'Clear all', @(~, ~) clearCuts());
    btn([0.74 0.95 0.07 0.04], 'Show all', @(~, ~) xlim(ax1, xFull));
    hSave = btn([0.815 0.95 0.17 0.04], 'Save trimmed data', @(~, ~) doSave());
    set(hSave, 'FontWeight', 'bold', 'BackgroundColor', [0.08 0.75 0.08]);

    % --- threshold sliders: one vertical slider on the right of each plot ---
    lbl = @(pos, col) uicontrol(figT, 'Style', 'text', 'Units', 'normalized', 'Position', pos, ...
        'BackgroundColor', [0.08 0.08 0.08], 'ForegroundColor', col, 'FontSize', 10, ...
        'FontWeight', 'bold', 'HorizontalAlignment', 'center');
    % Motion: the slider spans exactly the plot's height and y-range, so the
    % knob sits level with the dashed line it moves.
    ylM = ylim(ax1);
    hMotSld = uicontrol(figT, 'Style', 'slider', 'Units', 'normalized', ...
        'Position', [posA1(1) + posA1(3) + 0.012, posA1(2), 0.016, posA1(4)], ...
        'Min', ylM(1), 'Max', ylM(2), 'Value', mt, 'SliderStep', [0.005 0.05] / diff(ylM));
    hMotLbl = lbl([0.885 posA1(2) + posA1(4) + 0.005 0.11 0.035], [1 0.45 0.45]);
    % Closed threshold: an OFFSET below the moving baseline, so it cannot sit
    % beside one y value. Slider DOWN = line further down (= +more px).
    hCentSld = uicontrol(figT, 'Style', 'slider', 'Units', 'normalized', ...
        'Position', [posA2(1) + posA2(3) + 0.012, posA2(2), 0.016, posA2(4)], ...
        'Min', -40, 'Max', -2, 'Value', -cd, 'SliderStep', [0.5 2] / 38);
    hCentLbl = lbl([0.885 posA2(2) + posA2(4) + 0.005 0.11 0.035], [1 0.6 0.3]);
    for sld = [hMotSld hCentSld]
        addlistener(sld, 'ContinuousValueChange', @(~, ~) moveVirtual());
    end
    set([hMotSld hCentSld], 'Callback', @(~, ~) moveVirtual());

    uicontrol(figT, 'Style', 'text', 'Units', 'normalized', 'Position', [0.06 0.462 0.42 0.04], ...
        'BackgroundColor', [0.08 0.08 0.08], 'ForegroundColor', [0.85 0.85 0.85], ...
        'HorizontalAlignment', 'left', 'FontSize', 10, 'String', ...
        {'Thresholds: move a slider on the right to shift the DASHED line,', ...
         'then press Compute. The solid line is what the counts use.'});
    hComp = btn([0.50 0.462 0.14 0.042], 'Compute', @(~, ~) commit());
    set(hComp, 'FontWeight', 'bold', 'BackgroundColor', [0.25 0.55 1.0], 'ForegroundColor', 'w');
    btn([0.65 0.462 0.16 0.042], 'Back to recorded', @(~, ~) resetThr());

    hStatus = uicontrol(figT, 'Style', 'text', 'Units', 'normalized', 'Position', [0.06 0.005 0.91 0.035], ...
        'BackgroundColor', [0.08 0.08 0.08], 'ForegroundColor', [1 0.8 0.3], ...
        'HorizontalAlignment', 'left', 'FontSize', 11);

    % Offline counts at the recorded thresholds, so the jump between the
    % recorded blinks (used at exactly 0.18 / +18) and re-detection is visible.
    offRef = [numel(detectMotion(D.timeLog, D.diffLog, 0.18, D.suppressLog == 1)), ...
              numel(detectCentroid(D.timeLog, D.centYampLog, D.baseYlog, 18, D.suppressLog == 1))];
    for k = 1:size(cuts, 1), regions{end+1, 1} = drawCut(cuts(k, :)); end %#ok<AGROW>
    refresh(); moveVirtual();
    set(figT, 'WindowButtonDownFcn', @down, 'WindowButtonMotionFcn', @move, ...
              'WindowButtonUpFcn', @up);

    function Dt = current()
        Dt = applyThresholds(D, cd, mt);
    end

    function [m, c] = candidate()
        m = max(round(get(hMotSld, 'Value') * 200) / 200, 0.01);   % 0.005 steps
        c = -round(get(hCentSld, 'Value') * 2) / 2;                           % 0.5 px steps
    end

    function moveVirtual()
        % Cheap: only the dashed lines and labels move. No recount.
        [m, c] = candidate();
        set(hMotV, 'Value', m);
        set(hClosedV, 'YData', Y(3, :) + c);
        set(hMotLbl,  'String', sprintf('New: %.3g', m));
        set(hCentLbl, 'String', sprintf('New: base + %g px', c));
        pending = abs(m - mt) > 1e-9 || abs(c - cd) > 1e-9;
        if pending
            set(hComp, 'String', 'Compute *', 'BackgroundColor', [1 0.55 0.1]);
        else
            set(hComp, 'String', 'Compute', 'BackgroundColor', [0.25 0.55 1.0]);
        end
    end

    function refresh()
        % Solid lines, blink ticks and counts for the APPLIED thresholds.
        set(hMotThr, 'Value', mt);
        set(hClosed, 'YData', Y(3, :) + cd);
        Dt = current();
        set(hMotTicks,  'XData', D.t0 + seconds(Dt.blinkTimestamps), ...
                        'YData', motTickY * ones(size(Dt.blinkTimestamps)));
        set(hCentTicks, 'XData', D.t0 + seconds(Dt.centBlinkTimestamps), ...
                        'YData', 3 * ones(size(Dt.centBlinkTimestamps)));
        updateStatus(sprintf('Applied: motion %.3g, closed base + %g px.', mt, cd));
    end

    function commit()
        [mt, cd] = candidate();
        refresh(); moveVirtual();
        updateStatus('Updating the other windows...'); drawnow;
        Dv = applyCutsD(current(), mergeCuts(cuts));
        if ~isempty(cuts) || abs(cd - 18) > 1e-9 || abs(mt - 0.18) > 1e-9
            Dv.note = 'PREVIEW, not saved';
        end
        plotAll(Dv, folder);
        figure(figT);
        updateStatus(sprintf('Applied: motion %.3g, closed base + %g px. Other windows updated (press Save to keep).', mt, cd));
    end

    function resetThr()
        % Moves the dashed lines back; Compute applies it like any other value.
        set(hMotSld, 'Value', 0.18); set(hCentSld, 'Value', -18);
        moveVirtual();
    end

    function h = drawCut(c)
        x = D.t0 + seconds(c);
        h = [xregion(ax1, x(1), x(2), 'FaceColor', [1 0.15 0.15], 'FaceAlpha', 0.35), ...
             xregion(ax2, x(1), x(2), 'FaceColor', [1 0.15 0.15], 'FaceAlpha', 0.35)];
    end

    function s = pointerSec()
        % Seconds under the pointer if it is inside either plot, else [].
        s = [];
        for axk = [ax1 ax2]
            cp = axk.CurrentPoint;
            xl = ruler2num(xlim(axk), axk.XAxis); yl = ylim(axk);
            if cp(1, 1) >= xl(1) && cp(1, 1) <= xl(2) && cp(1, 2) >= yl(1) && cp(1, 2) <= yl(2)
                s = seconds(num2ruler(cp(1, 1), axk.XAxis) - D.t0);
                return;
            end
        end
    end

    function down(~, ~)
        s = pointerSec();
        if isempty(s), return; end
        drag = struct('s0', s, 'h', drawCut([s s]));
    end

    function move(~, ~)
        if isempty(drag), return; end      % cheap when not dragging: fires on every mouse move
        s = pointerSec();
        if isempty(s), return; end
        set(drag.h, 'Value', D.t0 + seconds(sort([drag.s0 s])));
    end

    function up(~, ~)
        if isempty(drag), return; end
        s = pointerSec();
        if isempty(s), s = drag.s0; end
        cc = sort([drag.s0 s]);
        if cc(2) - cc(1) < 0.2        % a click, not a drag
            delete(drag.h);
        else
            set(drag.h, 'Value', D.t0 + seconds(cc));
            cuts(end+1, :) = cc;
            regions{end+1, 1} = drag.h;
            updateStatus('');
        end
        drag = [];
    end

    function undoCut()
        if isempty(cuts), return; end
        delete(regions{end}); regions(end) = []; cuts(end, :) = [];
        updateStatus('');
    end

    function clearCuts()
        if isempty(cuts), return; end
        if ~strcmp(questdlg(sprintf('Remove all %d cuts?', size(cuts, 1)), 'Clear cuts', ...
                'Remove', 'Cancel', 'Cancel'), 'Remove'), return; end
        cellfun(@delete, regions); regions = cell(0, 1); cuts = zeros(0, 2);
        updateStatus('');
    end

    function doSave()
        updateStatus('Saving...'); drawnow;
        try
            [kept, removed] = saveTrimmed(current(), mergeCuts(cuts), folder);
            plotAll(loadTrimmed(folder), folder);
            figure(figT);
            updateStatus(sprintf('SAVED: kept %.1f min, removed %.1f min. FrankensteinData updated.', ...
                kept, removed));
        catch err
            updateStatus(['SAVE FAILED: ' err.message]);
            fprintf(2, '%s\n', getReport(err, 'extended', 'hyperlinks', 'off'));
        end
    end

    function updateStatus(extra)
        m = mergeCuts(cuts);
        Dt = current();
        nM = nnz(~inCuts(Dt.blinkTimestamps, m)); nC = nnz(~inCuts(Dt.centBlinkTimestamps, m));
        str = sprintf(['%d cut(s), %.1f min selected.   Blinks outside cuts: motion %d, centroid %d.   ' ...
            'Recorded live: %d / %d (offline at 0.18 / +18: %d / %d -- no head-move filter offline)'], ...
            size(cuts, 1), sum(diff(m, 1, 2)) / 60, nM, nC, D.rec.blinkCount, D.rec.centBlinkCount, ...
            offRef(1), offRef(2));
        if ~isempty(extra), str = [str '   ' extra]; end
        set(hStatus, 'String', str);
    end
end

function [keptMin, removedMin] = saveTrimmed(D, cuts, folder)
    % Originals are only READ here. Everything written is a trimmed_* file,
    % fully regenerated from the originals + cut list + thresholds each save.
    T = applyCutsD(D, cuts);
    S = streamDefs();
    for i = 1:size(S, 1)
        writeStream(fullfile(folder, ['trimmed_' S{i, 1} '.csv']), D.t0, S{i, 2}, streamMatrix(T, S(i, :)));
    end
    writeStimuli(fullfile(folder, 'trimmed_stimuli.csv'), D.t0, D.stimTimes, D.stimNames);   % never cut
    fid = fopen(fullfile(folder, 'trimmed_cuts.csv'), 'w');
    fprintf(fid, 'cut_start_wall,cut_end_wall,cut_start_s,cut_end_s\n');
    for c = 1:size(cuts, 1)
        w = D.t0 + seconds(cuts(c, :));
        w.Format = 'yyyy-MM-dd HH:mm:ss.SSS';
        fprintf(fid, '%s,%s,%.4f,%.4f\n', char(w(1)), char(w(2)), cuts(c, 1), cuts(c, 2));
    end
    fclose(fid);
    fid = fopen(fullfile(folder, 'analysis_settings.csv'), 'w');
    fprintf(fid, 'closed_delta_px,motion_threshold\n%.4g,%.4g\n', D.closedDelta, D.motionThr);
    fclose(fid);
    keptMin = T.recMin;
    removedMin = sum(coverageSec(D.timeLog)) / 60 - keptMin;
    fprintf('Trimmed data saved in %s (%d cuts, %.1f min removed).\n', folder, size(cuts, 1), removedMin);
    rebuildFrankenstein(fileparts(folder));
end

function rebuildFrankenstein(root)
    % Regenerated from scratch every save: it is derived data, so a re-trim
    % of any session simply flows through.
    dd = dir(fullfile(root, 'session_*'));
    dd = dd([dd.isdir]);
    dd = dd(arrayfun(@(x) hasTrimmed(fullfile(root, x.name)), dd));
    if isempty(dd), return; end
    T = cell(numel(dd), 1);
    for k = 1:numel(dd), T{k} = loadTrimmed(fullfile(root, dd(k).name)); end
    [~, o] = sort(cellfun(@(x) x.t0, T)); T = [T{o}]; dd = dd(o);
    t0g = T(1).t0;
    out = fullfile(root, 'FrankensteinData');
    if ~isfolder(out), mkdir(out); end
    S = streamDefs();
    for i = 1:size(S, 1)
        M = zeros(0, 1 + numel(S{i, 4}));
        for k = 1:numel(T)
            Mk = streamMatrix(T(k), S(i, :));
            Mk(:, 1) = Mk(:, 1) + seconds(T(k).t0 - t0g);
            M = [M; Mk]; %#ok<AGROW>
        end
        writeStream(fullfile(out, ['trimmed_' S{i, 1} '.csv']), t0g, S{i, 2}, M);
    end
    st = arrayfun(@(x) x.stimTimes + seconds(x.t0 - t0g), T, 'UniformOutput', false);
    writeStimuli(fullfile(out, 'trimmed_stimuli.csv'), t0g, [st{:}], [T.stimNames]);
    fid = fopen(fullfile(out, 'sources.csv'), 'w');
    fprintf(fid, 'session,start_wall,end_wall,kept_min\n');
    for k = 1:numel(T)
        e = T(k).t0 + seconds(max([T(k).timeLog, 0])); s = T(k).t0;
        s.Format = 'yyyy-MM-dd HH:mm:ss'; e.Format = s.Format;
        fprintf(fid, '%s,%s,%s,%.2f\n', dd(k).name, char(s), char(e), T(k).recMin);
    end
    fclose(fid);
    fid = fopen(fullfile(out, 'README.txt'), 'w');
    fprintf(fid, ['FrankensteinData -- the trimmed parts of every trimmed session, stitched\n' ...
                  'on one clock. Rebuilt automatically each time a trim is saved.\n' ...
                  'Files: trimmed_<stream>.csv  (wall_time, time_s since %s, data).\n' ...
                  'sources.csv lists the sessions included.\n' ...
                  'Originals stay in each session folder, untouched.\n' ...
                  'Plot: Plot_Captured_Eye_Data_V6_Win_to_Mac(''%s'')\n'], char(t0g), out);
    fclose(fid);
    fprintf('FrankensteinData rebuilt from %d session(s): %s\n', numel(T), out);
end

function writeStream(file, t0, header, M)
    % wall_time, time_s, data columns. One fprintf for the whole
    % matrix: building per-row date strings is ~300x slower at 200k rows.
    fid = fopen(file, 'w');
    if fid < 0, error('Cannot write %s', file); end
    c = onCleanup(@() fclose(fid));
    fprintf(fid, 'wall_time,%s\n', header);
    if isempty(M), return; end
    w = t0 + seconds(M(:, 1));
    [y, mo, d] = ymd(w); [h, mi, s] = hms(w);
    s = floor(s * 1000) / 1000;    % never round 59.9996 up to "60.000"
    fmt = ['%04d-%02d-%02d %02d:%02d:%06.3f,%.4f', repmat(',%.10g', 1, size(M, 2) - 1), '\n'];
    fprintf(fid, fmt, [y, mo, d, h, mi, s, M].');
end

function cuts = readCuts(folder)
    cuts = zeros(0, 2);
    f = fullfile(folder, 'trimmed_cuts.csv');
    if ~isfile(f), return; end
    M = readmatrix(f, 'NumHeaderLines', 1);
    if ~isempty(M), cuts = M(:, 3:4); end
end

function m = mergeCuts(cuts)
    m = zeros(0, 2);
    if isempty(cuts), return; end
    cuts = sortrows(sort(cuts, 2));
    m = cuts(1, :);
    for k = 2:size(cuts, 1)
        if cuts(k, 1) <= m(end, 2)
            m(end, 2) = max(m(end, 2), cuts(k, 2));
        else
            m(end+1, :) = cuts(k, :); %#ok<AGROW>
        end
    end
end

function k = inCuts(t, cuts)
    k = false(size(t));
    for c = 1:size(cuts, 1)
        k = k | (t >= cuts(c, 1) & t <= cuts(c, 2));
    end
end

function tf = hasTrimmed(folder)
    tf = isfile(fullfile(folder, 'trimmed_cuts.csv'));
end

function tf = isFrankenstein(folder)
    tf = isfile(fullfile(folder, 'sources.csv')) && isfile(fullfile(folder, 'trimmed_frames.csv'));
end

% =========================================================================
% PER-MINUTE BINS (recomputed for trimmed / stitched data)
% =========================================================================
function c = coverageSec(t)
    % Seconds of recording each frame stands for (gap to the next, capped at
    % 1 s so cuts and run boundaries count as not recorded).
    t = t(:).';
    if numel(t) < 2, c = zeros(size(t)); return; end
    c = [min(diff(t), 1), 0];
end

function D = minuteBins(D)
    t = D.timeLog(:).';
    if isempty(t)
        [D.minBinCounts, D.minBinCentCounts, D.minBinAvgDur, D.minBinMaxDur, ...
         D.minBinTempObj, D.minBinTempAmb] = deal([]);
        return;
    end
    nMin = max(1, ceil(max(t) / 60));
    bin  = @(x) min(max(floor(x / 60) + 1, 1), nMin);
    cov  = accumarray(bin(t).', coverageSec(t).', [nMin 1]).';
    ok   = cov >= 15;                         % < 15 s left in a minute: no value
    scale = ones(1, nMin);
    part = ok & cov < 50;                     % partly cut minute -> rate per minute
    scale(part) = 60 ./ cov(part);
    cnt = @(x) accumarray(bin(x(:)), 1, [nMin 1]).';
    D.minBinCounts = cnt(D.blinkTimestamps) .* scale;
    D.minBinCentCounts = cnt(D.centBlinkTimestamps) .* scale;
    D.minBinCounts(~ok) = NaN; D.minBinCentCounts(~ok) = NaN;
    dur = D.centBlinkDur(:).'; tb = D.centBlinkTimestamps(:).';
    good = ~isnan(dur);
    D.minBinAvgDur = accumarray(bin(tb(good)).', dur(good).', [nMin 1], @mean, NaN).';
    D.minBinMaxDur = accumarray(bin(tb(good)).', dur(good).', [nMin 1], @max, NaN).';
    D.minBinTempObj = binByMinute(D.tempTimeLog, D.tempObjLog, nMin);
    D.minBinTempAmb = binByMinute(D.tempTimeLog, D.tempAmbLog, nMin);
    D.minBinBpm = binByMinute(D.bpmTimeLog, D.bpmValLog, nMin);
end

function b = binByMinute(tv, vv, nMin)
% Mean of vv per one-minute bin. NaN for an empty minute.
    if isempty(tv) || isempty(vv), b = nan(1, nMin); return; end
    n = min(numel(tv), numel(vv));
    tv = tv(1:n); vv = vv(1:n);
    if nMin < 1, nMin = ceil(max(tv) / 60); end
    k = floor(tv(:) / 60) + 1;
    sel = k >= 1 & k <= nMin & ~isnan(vv(:));
    b = accumarray(k(sel), vv(sel), [nMin 1], @mean, NaN).';
end

% =========================================================================
% PLOTTING
% =========================================================================
function plotAll(D, folder)
    % Replaces this session's analysis windows (e.g. after a save) and leaves
    % the trim window alone.
    tag = ['eyeplot|' folder];
    delete(findall(groot, 'Type', 'figure', 'Tag', tag));
    [~, name] = fileparts(folder);
    if ~isempty(D.note), name = [name '  (' D.note ')'];
    elseif D.trimmed,    name = [name '  (TRIMMED)']; end
    figs = plotEyeFigure(D, name);
    try
        figs = [figs, plotSensorFigure(D, name)];
    catch err
        warning('%s', ['Sensor figure failed: ' err.message]);
    end
    try
        figs = [figs, plotPerMinute(D, name)];
    catch err
        warning('%s', ['Per-minute figure failed: ' err.message]);
    end
    set(figs(isgraphics(figs)), 'Tag', tag);
end

function h = tline(ax, D, t, y, maxGap, varargin)
    % Time series on the clock axis. Min/max-decimated to <= 8000 points per
    % line (every spike survives at screen resolution) and the pen lifted
    % across gaps. Sessions draw 100k-2M points otherwise; that much geometry
    % is what made MATLAB hang when one of these windows was closed.
    [t, y] = minmaxDecimate(t, y, 4000);
    [t, y] = breakGaps(t, y, maxGap);
    h = plot(ax, clk(D, t), y, varargin{:});
end

function [t, y] = minmaxDecimate(t, y, nb)
    t = t(:).'; y = y(:).';
    n = numel(y);
    if n <= 2 * nb, return; end
    b = ceil(n / nb);
    Y = nan(b, ceil(n / b)); Y(1:n) = y;
    [~, imn] = min(Y, [], 1); [~, imx] = max(Y, [], 1);
    off = (0:size(Y, 2) - 1) * b;
    i = unique([imn + off, imx + off]);
    i = i(i <= n);
    t = t(i); y = y(i);
end

function c = clk(D, t)
    c = D.t0 + seconds(t);
end

function darkAx(ax)
    set(ax, 'Color', [0.13 0.13 0.13], 'XColor', 'w', 'YColor', 'w', ...
        'GridColor', [0.35 0.35 0.35], 'GridAlpha', 0.6);
    hold(ax, 'on'); grid(ax, 'on');
end

function leg(ax, loc)
    if nargin < 2, loc = 'northeast'; end
    legend(ax, 'Location', loc, 'TextColor', 'w', 'FontSize', 7, ...
        'Color', [0.10 0.10 0.10], 'EdgeColor', [0.45 0.45 0.45]);
end

function clockTicks(axs, t0, nMin)
    % Per-minute axes: bar k is minute k of the recording; label it with the
    % clock time that minute started.
    tk = minuteTicks(nMin);
    lab = cellstr(string(t0 + minutes(tk - 1), 'HH:mm'));
    for a = axs(:).'
        set(a, 'XTick', tk, 'XTickLabel', lab);
        xlabel(a, 'Clock time (minute start)', 'Color', 'w', 'FontSize', 8);
    end
end

function t = minuteTicks(nMin)
    steps = [1 2 5 10 15 30 60 120 240];
    st = steps(find(nMin ./ steps <= 14, 1));
    if isempty(st), st = steps(end); end
    t = 1:st:max(nMin, 1);
end

function [t, Y] = breakGaps(t, Y, maxGap)
    % NaN after every gap longer than maxGap so plot() lifts the pen instead
    % of drawing a straight line across a cut or a dropout. Y: rows = series.
    t = t(:).';
    if size(Y, 2) ~= numel(t), Y = Y.'; end
    g = find(diff(t) > maxGap);
    if isempty(g), return; end
    idx = (1:numel(t)) + [0, cumsum(ismember(1:numel(t)-1, g))];
    t2 = nan(1, numel(t) + numel(g)); t2(idx) = t; t = t2;
    Y2 = nan(size(Y, 1), numel(t)); Y2(:, idx) = Y; Y = Y2;
end

function fig = plotEyeFigure(D, folderName)
    nT = numel(D.timeLog);
    closedDeltaPx = D.closedDelta;
    fig = figure('Name', sprintf('Eye Data: %s', folderName), ...
                 'NumberTitle', 'off', 'Color', [0.08 0.08 0.08], ...
                 'Position', [60 40 1500 1040]);
    if D.isV2, ampY = D.centYampLog; else, ampY = D.lidYlog; end
    xr = [];
    if nT > 0, xr = [clk(D, min(D.timeLog)), clk(D, max(D.timeLog))]; end

    % ---------- (1) Motion signal + blink ticks ----------
    ax1 = subplot(4, 2, 1); darkAx(ax1);
    if nT > 0, tline(ax1, D, D.timeLog, D.diffLog, 1.0, 'm-', 'LineWidth', 0.6, 'DisplayName', 'Motion signal'); end
    yline(ax1, D.motionThr, 'w:', 'LineWidth', 1.2, 'DisplayName', sprintf('Detection threshold (%.2f)', D.motionThr));
    % Ticks along the top, not full-height lines: hundreds of xlines over a
    % long session painted the whole panel green and hid the signal.
    if ~isempty(D.blinkTimestamps)
        plot(ax1, clk(D, D.blinkTimestamps), 1.12 * ones(size(D.blinkTimestamps)), 'g|', ...
             'MarkerSize', 7, 'DisplayName', 'Motion blink');
    end
    if ~isempty(D.centBlinkTimestamps)
        plot(ax1, clk(D, D.centBlinkTimestamps), 1.02 * ones(size(D.centBlinkTimestamps)), 'c|', ...
             'MarkerSize', 7, 'DisplayName', 'Centroid blink');
    end
    xlabel(ax1, 'Clock time', 'Color', 'w');
    ylabel(ax1, 'Frame Diff (norm.)', 'Color', 'w');
    title(ax1, sprintf('Motion Signal — motion %d / centroid %d blinks', ...
          numel(D.blinkTimestamps), numel(D.centBlinkTimestamps)), 'Color', 'w', 'FontWeight', 'bold');
    if ~isempty(xr), xlim(ax1, xr); end
    ylim(ax1, [-0.05, 1.2]); leg(ax1); markEvents(ax1, D);

    % ---------- (2) Centroid vs baseline ----------
    ax2 = subplot(4, 2, 2); darkAx(ax2); set(ax2, 'YDir', 'reverse');
    if nT > 0
        tline(ax2, D, D.timeLog, ampY, 1.0, 'g-', 'LineWidth', 0.6, 'DisplayName', 'Amplified centroid Y (drives detector)');
        tline(ax2, D, D.timeLog, D.baseYlog, 1.0, 'y-', 'LineWidth', 1.0, 'DisplayName', 'Rolling baseline');
        tline(ax2, D, D.timeLog, D.baseYlog + closedDeltaPx, 1.0, '-', 'Color', [1 0.45 0.1], 'LineWidth', 1.0, ...
             'DisplayName', sprintf('Closed threshold (base + %g)', closedDeltaPx));
    end
    if ~isempty(D.centBlinkTimestamps)
        plot(ax2, clk(D, D.centBlinkTimestamps), 3 * ones(size(D.centBlinkTimestamps)), 'c|', ...
             'MarkerSize', 7, 'DisplayName', 'Centroid blink (threshold cross)');
    end
    ylabel(ax2, 'Centroid Y (px,  0 = top)', 'Color', 'w');
    title(ax2, 'Amplified Centroid vs Baseline  (down = closing)', 'Color', 'w', 'FontWeight', 'bold');
    xlabel(ax2, 'Clock time', 'Color', 'w');
    ylim(ax2, [0, 80]);
    if ~isempty(xr), xlim(ax2, xr); end
    leg(ax2); markEvents(ax2, D);

    nMin = numel(D.minBinCounts);
    % ---------- (3) Per-minute blink count ----------
    ax3 = subplot(4, 2, 3); darkAx(ax3);
    if nMin > 0
        motCnt = D.minBinCounts(:);
        centCnt = padtrim(D.minBinCentCounts, nMin); centCnt = centCnt(:);
        bb3 = bar(ax3, 1:nMin, [motCnt, centCnt], 'EdgeColor', 'none', 'BarWidth', 1);
        bb3(1).FaceColor = [0.30 0.85 0.45];
        bb3(2).FaceColor = [0.30 0.80 0.95];
        bb3(1).DisplayName = sprintf('Motion (total %d)',   numel(D.blinkTimestamps));
        bb3(2).DisplayName = sprintf('Centroid (total %d)', numel(D.centBlinkTimestamps));
        leg(ax3); xlim(ax3, [0.5, nMin + 0.5]);
    end
    ylabel(ax3, 'Blinks per Minute', 'Color', 'w');
    title(ax3, 'Blink Count per Minute — Motion vs Centroid', 'Color', 'w', 'FontWeight', 'bold');

    % ---------- (4) Per-minute avg & max blink duration ----------
    ax4 = subplot(4, 2, 4); darkAx(ax4);
    if ~isempty(D.minBinAvgDur)
        n4 = numel(D.minBinAvgDur);
        avgDur = D.minBinAvgDur; avgDur(isnan(avgDur)) = 0;
        maxDur = padtrim(D.minBinMaxDur, n4); maxDur(isnan(maxDur)) = 0;
        bb = bar(ax4, 1:n4, [avgDur(:), maxDur(:)], 'EdgeColor', 'none', 'BarWidth', 1);
        bb(1).FaceColor = [0.30 0.85 0.45]; bb(1).DisplayName = 'Avg duration';
        bb(2).FaceColor = [1.00 0.55 0.20]; bb(2).DisplayName = 'Max duration';
        leg(ax4); xlim(ax4, [0.5, max(nMin, n4) + 0.5]);
    end
    ylabel(ax4, 'Duration (s)', 'Color', 'w');
    title(ax4, 'Blink Duration per Minute', 'Color', 'w', 'FontWeight', 'bold');

    % ---------- (5) Gaze per minute ----------
    ax5 = subplot(4, 2, 5); darkAx(ax5);
    g5 = D.minBinGazeShifts; f5 = D.minBinFixDur; n5 = numel(g5);
    if any(~isnan(g5))
        yyaxis(ax5, 'left'); hold(ax5, 'on');
        bar(ax5, 1:n5, g5, 0.7, 'FaceColor', [1 0.6 0], 'EdgeColor', 'none', 'DisplayName', 'Gaze shifts per minute');
        ylabel(ax5, 'Shifts per minute'); set(ax5, 'YColor', [1 0.6 0]);
        ylim(ax5, [0, 1.25 * max([g5, 1], [], 'omitnan')]);
        if any(~isnan(f5))
            yyaxis(ax5, 'right'); hold(ax5, 'on');
            plot(ax5, 1:n5, f5, 'o-', 'Color', [0.4 0.8 1], 'LineWidth', 1.4, 'MarkerFaceColor', [0.4 0.8 1], ...
                 'MarkerSize', 4, 'DisplayName', 'Typical fixation (s)');
            ylabel(ax5, 'Fixation (s)'); set(ax5, 'YColor', [0.4 0.8 1]);
            ylim(ax5, [0, 1.3 * max(f5, [], 'omitnan')]);
            yyaxis(ax5, 'left');
        end
        leg(ax5); xlim(ax5, [0.5, max(nMin, n5) + 0.5]);
    else
        text(ax5, 0.5, 0.5, 'No gaze data', 'Units', 'normalized', 'Color', [0.6 0.6 0.6], ...
             'HorizontalAlignment', 'center');
    end
    gt = 'Gaze: shifts per minute (bars), fixation length (line)';
    if ~strcmp(D.gazeSource, 'pupil'), gt = [gt '  [' D.gazeSource ']']; end
    title(ax5, gt, 'Color', 'w', 'FontWeight', 'bold');
    clockTicks([ax3 ax4 ax5], D.t0, max([nMin, numel(D.minBinAvgDur), n5]));

    % ---------- (6) Summary ----------
    ax6 = subplot(4, 2, 6);
    set(ax6, 'Color', [0.13 0.13 0.13], 'XColor', 'none', 'YColor', 'none');
    hold(ax6, 'on'); xlim(ax6, [0 1]); ylim(ax6, [0 1]);
    durMin = D.recMin;
    totalBlinks = numel(D.blinkTimestamps); totalCent = numel(D.centBlinkTimestamps);
    if ~D.trimmed && isfield(D, 'blinkCount'), totalBlinks = D.blinkCount; end
    avgBPM = 0; if durMin > 0, avgBPM = totalBlinks / durMin; end
    avgDurAll = NaN; maxDurAll = NaN;
    if ~isempty(D.blinkDurations), avgDurAll = mean(D.blinkDurations); maxDurAll = max(D.blinkDurations); end
    if avgBPM > 25,    fState = 'TIRED';   fCol = [1.0 0.4 0.4];
    elseif avgBPM < 6, fState = 'FOCUSED'; fCol = [1.0 0.85 0.3];
    else,              fState = 'NORMAL';  fCol = [0.4 1.0 0.4];
    end
    t1 = D.t0; t2 = D.t0 + seconds(max([D.timeLog, 0]));
    t1.Format = 'yyyy-MM-dd HH:mm'; t2.Format = 'HH:mm';
    text(ax6, 0.02, 0.96, sprintf('Session: %s', folderName), ...
        'Color', [0.85 0.85 1.0], 'FontSize', 11, 'FontWeight', 'bold', 'Interpreter', 'none');
    text(ax6, 0.02, 0.88, sprintf('Recorded: %s - %s', char(t1), char(t2)), ...
        'Color', [0.7 0.7 0.7], 'FontSize', 9, 'Interpreter', 'none');
    agree = 'n/a'; if totalBlinks > 0, agree = sprintf('%.0f%%', 100 * totalCent / totalBlinks); end
    statsStr = {
        sprintf('Time recorded:      %.2f min%s', durMin, trimTag(D))
        sprintf('Blinks (motion):    %d', totalBlinks)
        sprintf('Blinks (centroid):  %d', totalCent)
        sprintf('Agreement:          %s', agree)
        sprintf('Blinks per minute:  %.2f', avgBPM)
        sprintf('Avg blink duration: %.3f s', avgDurAll)
        sprintf('Max blink duration: %.3f s', maxDurAll)
        sprintf('Gaze:               %.0f shifts/min, fixation %.1f s', D.gaze.rate, D.gaze.fix)
        sprintf('Thresholds:         closed +%g px, motion %.2f', D.closedDelta, D.motionThr)
        sprintf('Pauses:             %d  (total %s)', numel(D.pauseStart), fmtDur(sum(D.pauseDur)))
        };
    for i = 1:numel(statsStr)
        text(ax6, 0.05, 0.80 - (i-1)*0.075, statsStr{i}, 'Color', 'w', 'FontSize', 10, 'FontName', 'Menlo');
    end
    text(ax6, 0.05, 0.06, sprintf('STATE: %s', fState), 'Color', fCol, 'FontSize', 14, 'FontWeight', 'bold');
    title(ax6, 'Session Summary', 'Color', 'w', 'FontWeight', 'bold');

    % ---------- (7) Centroid position + derivative ----------
    ax7 = subplot(4, 2, [7 8]); darkAx(ax7);
    posCol = [0.55 0.85 1.00]; derCol = [1.00 0.55 0.15];
    if D.isV2 && nT > 1
        t = D.timeLog(:).'; pos = D.centYlog(:).';
        dpos = [0, abs(diff(pos))];
        dpos([false, diff(t) > 1]) = NaN;               % no derivative across a cut
        dsm = movmean(dpos, 10, 'omitnan', 'SamplePoints', t);
        yyaxis(ax7, 'left');
        tline(ax7, D, t, pos, 1.0, '-', 'Color', posCol, 'LineWidth', 1.0, 'DisplayName', 'Centroid Y (position)');
        ylabel(ax7, 'Centroid Y (px)', 'Color', posCol); set(ax7, 'YColor', posCol);
        yyaxis(ax7, 'right');
        % Raw per-frame spikes dim (solid, not transparent: transparent lines
        % are very slow to redraw); the 10 s mean on top is the readable trend.
        tline(ax7, D, t, dpos, 1.0, '-', 'Color', derCol * 0.45, 'LineWidth', 0.5, ...
             'DisplayName', '|\Delta centroid Y| per frame (raw)');
        tline(ax7, D, t, dsm, 1.0, '-', 'Color', derCol, 'LineWidth', 1.6, ...
             'DisplayName', '|\Delta centroid Y|, 10 s mean');
        ylabel(ax7, '|\Delta centroid Y|  (px/frame)', 'Color', derCol); set(ax7, 'YColor', derCol);
        xlim(ax7, xr); leg(ax7); markEvents(ax7, D);
    else
        noData(ax7, 'No V2 centroid trace available');
    end
    xlabel(ax7, 'Clock time', 'Color', 'w');
    title(ax7, 'Centroid Position vs Frame-to-Frame Derivative  (velocity check)', ...
        'Color', 'w', 'FontWeight', 'bold');

    sgtitle(fig, sprintf('Eye Data Session — %s', folderName), ...
        'Color', 'w', 'FontSize', 14, 'FontWeight', 'bold', 'Interpreter', 'none');
    fprintf('Done. Motion %d / Centroid %d blinks over %.2f min recorded.\n', totalBlinks, totalCent, durMin);
end

function s = trimTag(D)
    if D.trimmed, s = ' (trimmed)'; else, s = ''; end
end

function fig = plotSensorFigure(D, folderName)
    haveHeart = ~isempty(D.heartTimeLog);
    haveTemp  = ~isempty(D.tempTimeLog);
    haveLdr   = ~isempty(D.ldrTimeLog);
    fig = gobjects(0);
    if ~(haveHeart || haveTemp || haveLdr)
        fprintf('No PPG / temperature / LDR data in this session — sensor figure skipped.\n');
        return;
    end
    fig = figure('Name', sprintf('Sensors: %s', folderName), ...
                 'NumberTitle', 'off', 'Color', [0.08 0.08 0.08], ...
                 'Position', [100 60 1500 900]);
    xr = [];
    if haveHeart, xr = clk(D, [min(D.heartTimeLog), max(D.heartTimeLog)]); end

    % ---------- (1) PPG waveform ----------
    axP1 = subplot(3, 2, 1); darkAx(axP1);
    if haveHeart
        tline(axP1, D, D.heartTimeLog, D.heartFiltLog, 0.5, '-', 'Color', [0.30 0.65 1.00], 'LineWidth', 0.5);
        xlim(axP1, xr);
    else
        noData(axP1, 'No PPG data');
    end
    ylabel(axP1, 'Filtered IR', 'Color', 'w'); xlabel(axP1, 'Clock time', 'Color', 'w');
    title(axP1, 'PPG Waveform (MAX30102)', 'Color', 'w', 'FontWeight', 'bold'); markEvents(axP1, D);

    % ---------- (2) Derivative + beats ----------
    axP2 = subplot(3, 2, 2); darkAx(axP2);
    if haveHeart
        tline(axP2, D, D.heartTimeLog, D.heartDerivLog, 0.5, '-', 'Color', [1.00 0.35 0.30], ...
              'LineWidth', 0.5, 'DisplayName', 'd(IR)/dt');
        if ~isempty(D.beatTimes)
            % Tick row, not markers on the trace: >1000 triangles buried it.
            % (Also avoids interp1 on the PPG times, which are not unique --
            % that error used to blank the rest of this figure.)
            yl = ylim(axP2);
            plot(axP2, clk(D, D.beatTimes), yl(2) * 0.92 * ones(size(D.beatTimes)), '|', ...
                 'Color', [1 1 0.3], 'MarkerSize', 6, 'LineStyle', 'none', ...
                 'DisplayName', sprintf('%d detected beats', numel(D.beatTimes)));
            ylim(axP2, yl);
        end
        xlim(axP2, xr); leg(axP2);
    else
        noData(axP2, 'No PPG data');
    end
    ylabel(axP2, 'd(IR)/dt', 'Color', 'w'); xlabel(axP2, 'Clock time', 'Color', 'w');
    title(axP2, 'First Derivative + Detected Beats', 'Color', 'w', 'FontWeight', 'bold'); markEvents(axP2, D);

    % ---------- (3) Heart rate ----------
    axP3 = subplot(3, 2, [3 4]); darkAx(axP3);
    if ~isempty(D.bpmTimeLog)
        % Pen lifted where no beat was accepted for > 10 s: a straight line
        % across a dropout looks like a real reading and is not one.
        tline(axP3, D, D.bpmTimeLog, D.bpmValLog, 10, '-', 'Color', [1 1 0.3], 'LineWidth', 1.2, ...
              'DisplayName', 'BPM (median of last 8 intervals)');
        mB = median(D.bpmValLog, 'omitnan');
        yline(axP3, mB, 'w:', 'LineWidth', 1.2, 'DisplayName', sprintf('session median %.0f', mB));
        ylim(axP3, [max(30, min(D.bpmValLog) - 10), min(220, max(D.bpmValLog) + 10)]);
        xlim(axP3, clk(D, [min(D.bpmTimeLog), max(D.bpmTimeLog)])); leg(axP3);
    else
        noData(axP3, 'No BPM data (no finger detected)');
    end
    ylabel(axP3, 'BPM', 'Color', 'w'); xlabel(axP3, 'Clock time', 'Color', 'w');
    title(axP3, 'Heart Rate', 'Color', 'w', 'FontWeight', 'bold'); markEvents(axP3, D);

    % ---------- (4) Temperature ----------
    axP4 = subplot(3, 2, 5); darkAx(axP4);
    if haveTemp
        tline(axP4, D, D.tempTimeLog, D.tempObjLog, 5, '-', 'Color', [1 0.45 0.2], 'LineWidth', 1.2, 'DisplayName', 'Object (skin)');
        tline(axP4, D, D.tempTimeLog, D.tempAmbLog, 5, '-', 'Color', [0.5 0.7 1], 'LineWidth', 1.0, 'DisplayName', 'Ambient');
        leg(axP4, 'best');
    else
        noData(axP4, 'No temperature data');
    end
    ylabel(axP4, '\circC', 'Color', 'w'); xlabel(axP4, 'Clock time', 'Color', 'w');
    title(axP4, 'Temperature (MLX90614)', 'Color', 'w', 'FontWeight', 'bold'); markEvents(axP4, D);

    % ---------- (5) LDR ----------
    axP5 = subplot(3, 2, 6); darkAx(axP5);
    if haveLdr
        tline(axP5, D, D.ldrTimeLog, D.ldrValLog, 2, '-', 'Color', [0.9 0.9 0.4], 'LineWidth', 1.0, 'DisplayName', 'LDR raw');
        leg(axP5, 'best');
    else
        noData(axP5, 'No LDR data');
    end
    ylabel(axP5, 'ADC counts', 'Color', 'w'); xlabel(axP5, 'Clock time', 'Color', 'w');
    title(axP5, 'Ambient Light (GL5528)', 'Color', 'w', 'FontWeight', 'bold'); markEvents(axP5, D);

    sgtitle(fig, sprintf('Sensors — %s', folderName), 'Color', 'w', ...
        'FontSize', 14, 'FontWeight', 'bold', 'Interpreter', 'none');

    if ~isempty(D.bpmValLog)
        fprintf('BPM: median %.0f  range %.0f-%.0f  (%d beats)\n', ...
            median(D.bpmValLog, 'omitnan'), min(D.bpmValLog), max(D.bpmValLog), numel(D.beatTimes));
    end
    if haveTemp
        fprintf('Temp object: %.1f-%.1f C   ambient: %.1f-%.1f C\n', ...
            min(D.tempObjLog), max(D.tempObjLog), min(D.tempAmbLog), max(D.tempAmbLog));
    end
end

function figM = plotPerMinute(D, folderName)
    nMin = max([numel(D.minBinCounts), numel(D.minBinCentCounts), 0]);
    tObj = D.minBinTempObj; tAmb = D.minBinTempAmb;
    if isempty(tObj) && ~isempty(D.tempTimeLog)
        tObj = binByMinute(D.tempTimeLog, D.tempObjLog, nMin);
        tAmb = binByMinute(D.tempTimeLog, D.tempAmbLog, nMin);
    end
    bpm = D.minBinBpm;
    if isempty(bpm), bpm = binByMinute(D.bpmTimeLog, D.bpmValLog, nMin); end
    M = struct('minutes', [], 'blinks', D.minBinCounts, 'blinksCent', D.minBinCentCounts, ...
               'avgDur', D.minBinAvgDur, 'maxDur', D.minBinMaxDur, 'gaze', D.minBinGazeShifts, 'fixDur', D.minBinFixDur, ...
               'bpm', bpm, 'tempObj', tObj, 'tempAmb', tAmb);
    figM = figure('Name', sprintf('Per-Minute Trends: %s', folderName), ...
                  'NumberTitle', 'off', 'Color', [0.08 0.08 0.08], 'Position', [140 80 1100 800]);
    PerMinutePanels(figM, M, sprintf('Per-Minute Trends — %s', folderName));
    clockTicks(findobj(figM, 'Type', 'axes'), D.t0, nMin);
end

function markEvents(ax, D)
    % Pink labelled line per stimulus; grey band + "PAUSE n  hh:mm
    % (duration)" for every pause, at the top of the axes (respecting a
    % reversed y-axis).
    for k = 1:numel(D.stimTimes)
        xline(ax, clk(D, D.stimTimes(k)), '-', D.stimNames{k}, 'Color', [1 0.4 0.75], ...
              'LineWidth', 1.5, 'FontSize', 9, 'FontWeight', 'bold', ...
              'LabelVerticalAlignment', 'middle', 'HandleVisibility', 'off');
    end
    if isempty(D.pauseStart), return; end
    yl = ylim(ax);
    if strcmp(get(ax, 'YDir'), 'reverse'), yTop = yl(1) + 0.03 * diff(yl); va = 'top';
    else, yTop = yl(2) - 0.03 * diff(yl); va = 'top'; end
    for k = 1:numel(D.pauseStart)
        x1 = clk(D, D.pauseStart(k)); x2 = clk(D, D.pauseStart(k) + D.pauseDur(k));
        xregion(ax, x1, x2, 'FaceColor', [0.65 0.65 0.65], 'FaceAlpha', 0.3, 'HandleVisibility', 'off');
        x1.Format = 'HH:mm';
        text(ax, x1 + (x2 - x1) / 2, yTop, sprintf('PAUSE %d\n%s  (%s)', k, char(x1), fmtDur(D.pauseDur(k))), ...
             'Color', [0.9 0.9 0.9], 'FontSize', 8, 'FontWeight', 'bold', ...
             'HorizontalAlignment', 'center', 'VerticalAlignment', va, 'HandleVisibility', 'off');
    end
    ylim(ax, yl);
end

function s = fmtDur(sec)
    if sec >= 3600
        s = sprintf('%d:%02d:%02d', floor(sec / 3600), floor(mod(sec, 3600) / 60), round(mod(sec, 60)));
    else
        s = sprintf('%d:%02d', floor(sec / 60), round(mod(sec, 60)));
    end
end

function noData(ax, msg)
    text(ax, 0.5, 0.5, msg, 'Color', [0.7 0.7 0.7], 'Units', 'normalized', ...
         'HorizontalAlignment', 'center', 'FontSize', 9);
end

% ============= helper: pad-or-trim a vector to length n ===================
function v = padtrim(v, n)
    if isempty(v), v = nan(1, n); return; end
    v = v(:).';
    if numel(v) < n
        v(end+1:n) = NaN;
    elseif numel(v) > n
        v = v(1:n);
    end
end

% =========================================================================
% FILE HELPERS
% =========================================================================
function f = sessionFiles(folder)
    % The final session .mat(s) the recorder writes: session_<tag>_<reason>.mat
    f = dir(fullfile(folder, 'session_*.mat'));
end
function writeStimuli(file, t0, times, names)
    % Names are free text, so a table (quotes commas) rather than writeStream.
    w = t0 + seconds(times(:)); w.Format = 'yyyy-MM-dd HH:mm:ss.SSS';
    writetable(table(w, times(:), names(:), 'VariableNames', {'wall_time', 'time_s', 'stimulus'}), file);
end

function [times, names] = readStimuli(file)
    times = zeros(1, 0); names = cell(1, 0);
    if ~isfile(file), return; end
    T = readtable(file, 'TextType', 'char', 'Delimiter', ',');
    if isempty(T), return; end
    times = T.time_s(:).'; names = cellstr(T.stimulus).';
end

function D = readStream(D, file, names)
    if ~isfile(file)
        for i = 1:numel(names), if ~isfield(D, names{i}), D.(names{i}) = []; end, end
        return;
    end
    M = readmatrix(file, 'NumHeaderLines', 1, 'Delimiter', ',');
    fid = fopen(file, 'r'); hdr = fgetl(fid); fclose(fid);
    if ischar(hdr) && startsWith(hdr, 'wall_time') && ~isempty(M)
        M = M(:, 2:end);        % wall_time reads back as NaN; time_s is column 1
    end
    if ~isempty(M), M = M(~isnan(M(:, 1)), :); end
    if isempty(M), M = zeros(0, numel(names)); end
    if size(M, 2) < numel(names), M(:, end+1:numel(names)) = NaN; end
    for i = 1:numel(names)
        D.(names{i}) = M(:, i).';
    end
end

function d = desktopPath()
    d = '';
    if ispc
        try
            d = winqueryreg('HKEY_CURRENT_USER', ...
                'Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders', 'Desktop');
        catch
        end
        if isempty(d) || ~isfolder(d)
            up = getenv('USERPROFILE');
            cands = {fullfile(up, 'OneDrive', 'Desktop'), fullfile(up, 'Desktop')};
            for i = 1:numel(cands)
                if isfolder(cands{i}), d = cands{i}; break; end
            end
        end
    else
        d = fullfile(getenv('HOME'), 'Desktop');
    end
    if ~isempty(d) && ~isfolder(d), d = ''; end
end

% =========================================================================
% PerMinutePanels -- embedded copy of Other/PerMinutePanels.m
% =========================================================================
function PerMinutePanels(figH, M, titleStr)
    fn = {'minutes','blinks','blinksCent','avgDur','maxDur','gaze','fixDur','bpm', 'tempObj','tempAmb'};
    for k = 1:numel(fn)
        if ~isfield(M, fn{k}), M.(fn{k}) = []; end
        M.(fn{k}) = M.(fn{k})(:).';
    end
    if isempty(M.minutes), M.minutes = 1:numel(M.blinks); end
    n = numel(M.minutes);
    clf(figH);
    if n == 0
        annotation(figH, 'textbox', [0.1 0.45 0.8 0.1], 'String', 'No complete minutes yet', ...
            'Color', [0.7 0.7 0.7], 'EdgeColor', 'none', 'HorizontalAlignment', 'center', 'FontSize', 14);
        return;
    end
    for k = 3:numel(fn)          % pad every series to the minute count
        M.(fn{k}) = padtrim(M.(fn{k}), n);
    end
    set(figH, 'Color', [0.08 0.08 0.08]);
    xr = [0.5, n + 0.5];

    ax1 = subplot(3, 2, 1, 'Parent', figH); darkAx(ax1);
    if anyReal(M.blinks)
        bar(ax1, M.minutes, M.blinks, 0.6, 'FaceColor', [0.2 0.75 0.3], 'EdgeColor', 'none', 'DisplayName', 'Motion method');
        if anyReal(M.blinksCent)
            plot(ax1, M.minutes, M.blinksCent, 'o-', 'Color', [1 0.8 0.2], 'LineWidth', 1.3, ...
                 'MarkerFaceColor', [1 0.8 0.2], 'MarkerSize', 4, 'DisplayName', 'Centroid method');
        end
        leg(ax1, 'best');
    else
        noData(ax1, 'No blink data');
    end
    ylabel(ax1, 'Blinks', 'Color', 'w'); xlim(ax1, xr);
    title(ax1, 'Blinks per Minute', 'Color', 'w', 'FontWeight', 'bold');

    ax2 = subplot(3, 2, 2, 'Parent', figH); darkAx(ax2);
    if anyReal(M.avgDur) || anyReal(M.maxDur)
        plot(ax2, M.minutes, M.avgDur, 'o-', 'Color', [0.3 0.7 1], 'LineWidth', 1.4, ...
             'MarkerFaceColor', [0.3 0.7 1], 'MarkerSize', 4, 'DisplayName', 'Mean');
        plot(ax2, M.minutes, M.maxDur, 's--', 'Color', [1 0.5 0.4], 'LineWidth', 1.0, ...
             'MarkerSize', 4, 'DisplayName', 'Max');
        leg(ax2, 'best');
    else
        noData(ax2, 'No duration data');
    end
    ylabel(ax2, 'Seconds', 'Color', 'w'); xlim(ax2, xr);
    title(ax2, 'Blink Duration per Minute', 'Color', 'w', 'FontWeight', 'bold');

    ax3 = subplot(3, 2, 3, 'Parent', figH); darkAx(ax3);
    if anyReal(M.gaze)
        yyaxis(ax3, 'left'); hold(ax3, 'on');
        bar(ax3, M.minutes, M.gaze, 0.6, 'FaceColor', [1 0.6 0], 'EdgeColor', 'none', ...
            'DisplayName', 'Gaze shifts per minute');
        ylabel(ax3, 'Shifts per minute'); set(ax3, 'YColor', [1 0.6 0]);
        ylim(ax3, [0, 1.25 * max([M.gaze, 1], [], 'omitnan')]);
        if anyReal(M.fixDur)
            yyaxis(ax3, 'right'); hold(ax3, 'on');
            plot(ax3, M.minutes, M.fixDur, 'o-', 'Color', [0.4 0.8 1], 'LineWidth', 1.4, ...
                 'MarkerFaceColor', [0.4 0.8 1], 'MarkerSize', 4, 'DisplayName', 'Typical fixation (s)');
            ylabel(ax3, 'Fixation (s)'); set(ax3, 'YColor', [0.4 0.8 1]);
            ylim(ax3, [0, 1.3 * max(M.fixDur, [], 'omitnan')]);
            yyaxis(ax3, 'left');
        end
        leg(ax3, 'best');
    else
        noData(ax3, 'No gaze data');
    end
    xlim(ax3, xr);
    title(ax3, 'Gaze: shifts per minute (bars), fixation length (line)', 'Color', 'w', 'FontWeight', 'bold');

    ax4 = subplot(3, 2, 4, 'Parent', figH); darkAx(ax4);
    if anyReal(M.bpm)
        plot(ax4, M.minutes, M.bpm, 'o-', 'Color', [1 1 0.3], 'LineWidth', 1.6, ...
             'MarkerFaceColor', [1 1 0.3], 'MarkerSize', 4, 'DisplayName', 'Mean BPM');
        mb = mean(M.bpm, 'omitnan');
        yline(ax4, mb, 'w:', 'LineWidth', 1.1, 'DisplayName', sprintf('session mean %.0f', mb));
        leg(ax4, 'best');
    else
        noData(ax4, 'No heart-rate data (finger off sensor?)');
    end
    ylabel(ax4, 'BPM', 'Color', 'w'); xlim(ax4, xr);
    title(ax4, 'Heart Rate per Minute', 'Color', 'w', 'FontWeight', 'bold');

    ax5 = subplot(3, 2, [5 6], 'Parent', figH); darkAx(ax5);
    if anyReal(M.tempObj) || anyReal(M.tempAmb)
        plot(ax5, M.minutes, M.tempObj, 'o-', 'Color', [1 0.45 0.2], 'LineWidth', 1.6, ...
             'MarkerFaceColor', [1 0.45 0.2], 'MarkerSize', 4, 'DisplayName', 'Object (skin)');
        plot(ax5, M.minutes, M.tempAmb, 's-', 'Color', [0.5 0.7 1], 'LineWidth', 1.2, ...
             'MarkerSize', 4, 'DisplayName', 'Ambient');
        leg(ax5, 'best');
    else
        noData(ax5, 'No temperature data');
    end
    ylabel(ax5, '\circC', 'Color', 'w'); xlim(ax5, xr);
    title(ax5, 'Temperature per Minute', 'Color', 'w', 'FontWeight', 'bold');

    if nargin >= 3 && ~isempty(titleStr)
        sgtitle(figH, titleStr, 'Color', 'w', 'FontSize', 14, 'FontWeight', 'bold', 'Interpreter', 'none');
    end
end

function tf = anyReal(v)
    tf = ~isempty(v) && any(~isnan(v));
end

% =========================================================================
% SELF-TEST  (no figures, no files outside tempdir)
% =========================================================================
function selfTest()
    % mergeCuts / inCuts
    assert(isequal(mergeCuts([5 8; 1 3; 2 4]), [1 4; 5 8]), 'merge overlapping cuts');
    assert(isequal(inCuts([0 1 2 5 9], [1 2; 8 10]), logical([0 1 1 0 1])), 'inCuts');

    % breakGaps lifts the pen at gaps only
    [t, Y] = breakGaps([0 1 2 10 11], [1 2 3 4 5], 1.5);
    assert(isequal(isnan(t), logical([0 0 0 1 0 0])) && isnan(Y(4)) && Y(5) == 4, 'breakGaps');

    % offline re-detection matches the live rules
    tt = 0:0.1:20;
    sm = 0.05 * ones(size(tt)); sm(tt > 5 & tt < 5.5) = 0.30; sm(tt > 9 & tt < 9.4) = 0.12;
    mb = detectMotion(tt, sm, 0.18);
    assert(isscalar(mb) && abs(mb - 5.5) < 1e-9, 'motion: only the big peak at 0.18');
    assert(numel(detectMotion(tt, sm, 0.10)) == 2, 'motion: lower threshold catches the small one');
    base = 40 * ones(size(tt)); amp = base;
    % Closures RAMP in real data (the live 0.5 EMA smooths the centroid), and
    % the re-open test is a causal 3-frame median -- both as in the recorder.
    at = @(x) abs(tt - x) < 1e-9;
    amp(at(2.8)) = 45; amp(at(2.9)) = 50; amp(at(3.0)) = 55;
    amp(tt > 3.05 & tt < 3.95) = 62;    % deep closure (+22 px)
    amp(at(7.8)) = 45; amp(at(7.9)) = 50;
    amp(tt > 7.95 & tt < 8.55) = 54;    % shallow closure (+14 px), never crosses 18
    [on, du] = detectCentroid(tt, amp, base, 18);
    % True closure 3.05-3.95 s; the causal 3-frame median ends it at 4.1 (was 4.2 with 5 frames).
    assert(isscalar(on) && abs(on - 3.1) < 1e-9 && abs(du - 1.0) < 1e-6, 'centroid: one closure at +18');
    [on2, ~] = detectCentroid(tt, amp, base, 12);
    assert(numel(on2) == 2 && abs(on2(2) - 8) < 1e-9, 'centroid: lowering the line counts the shallow closure');
    Dr = struct('timeLog', tt, 'diffLog', sm, 'centYampLog', amp, 'baseYlog', base, 'centYlog', amp);
    Dr.t0 = datetime(2026, 10, 23); Dr.trimmed = false;
    Dr = normalize(Dr, 'x');
    Dr.rec = struct('blinkTimestamps', 99, 'centBlinkTimestamps', 98, 'centBlinkDur', 0.5, ...
                    'blinkDurations', 0.5, 'blinkCount', 1, 'centBlinkCount', 1);
    A = applyThresholds(Dr, 12, 0.10);
    assert(numel(A.centBlinkTimestamps) == 2 && numel(A.blinkTimestamps) == 2, 're-detected');
    B = applyThresholds(A, 18, 0.18);
    assert(isequal(B.blinkTimestamps, 99) && isequal(B.centBlinkTimestamps, 98), 'defaults restore the RECORDED blinks');

    % logged head-move filter is honoured offline
    supM = false(size(tt)); supM(abs(tt - 5.5) < 1e-9) = true;
    assert(isempty(detectMotion(tt, sm, 0.18, supM)), 'motion blink dropped where the filter was on');
    supC = tt > 2.95 & tt < 3.15;
    on3 = detectCentroid(tt, amp, base, 18, supC);
    assert(isscalar(on3) && on3 > 3.1, 'centroid onset waits for the filter to clear');

    % decimation keeps every extreme
    y = sin(1:100000); y(54321) = 9; y(77) = -9;
    [~, yd] = minmaxDecimate(1:100000, y, 4000);
    assert(numel(yd) <= 8000 && max(yd) == 9 && min(yd) == -9, 'decimation keeps spikes');

    % write -> read round trip keeps clock time, NaNs and every row
    d = tempname; mkdir(d); c = onCleanup(@() rmdir(d, 's'));
    t0 = datetime(2026, 10, 23, 23, 59, 58.5);
    M = [0 1 NaN; 1.25 2 3; 3.5 4 5];
    writeStream(fullfile(d, 'trimmed_blinks_centroid.csv'), t0, 'time_s,duration_s', M(:, 1:2));
    writeStream(fullfile(d, 'trimmed_frames.csv'), t0, 'time_s,motion,centY,centYamp,baseY', ...
                [M(:, 1), M(:, 2:3), M(:, 2:3)]);
    assert(isequal(firstRowStart(fullfile(d, 'trimmed_frames.csv')), t0), 'clock survives (across midnight)');
    R = readStream(struct(), fullfile(d, 'trimmed_frames.csv'), {'timeLog', 'diffLog', 'centYlog', 'centYampLog', 'baseYlog'});
    assert(numel(R.timeLog) == 3 && isnan(R.centYlog(1)) && R.centYlog(3) == 5, 'NaN rows kept');

    % cut -> save -> load -> stitch, end to end on synthetic sessions
    root = fullfile(d, 'MyDataFolder'); mkdir(root);
    for s = 1:2
        D = struct();
        D.t0 = datetime(2026, 10, 23, 13 + s, 0, 0); D.t0.Format = 'yyyy-MM-dd HH:mm:ss.SSS';
        D.timeLog = 0:0.1:300; n = numel(D.timeLog);
        D.diffLog = rand(1, n); D.centYlog = 40 + rand(1, n); D.centYampLog = D.centYlog; D.baseYlog = 40 * ones(1, n);
        D.heartTimeLog = 0:0.01:300; D.heartFiltLog = sin(2*pi*1.1*D.heartTimeLog); D.heartDerivLog = D.heartFiltLog;
        D.ldrTimeLog = 0:0.1:300; D.ldrValLog = 500 * ones(size(D.ldrTimeLog)); D.ldrStateLog = 2 * ones(size(D.ldrTimeLog));
        D.tempTimeLog = 0:0.5:300; D.tempAmbLog = 30 * ones(size(D.tempTimeLog)); D.tempObjLog = 36 * ones(size(D.tempTimeLog));
        D.bpmTimeLog = 1:300; D.bpmValLog = 66 * ones(1, 300); D.beatTimes = 0.5:0.9:299;
        D.blinkTimestamps = 10:10:290; D.centBlinkTimestamps = 15:30:285; D.centBlinkDur = 0.2 * ones(1, 10);
        f = fullfile(root, sprintf('session_2026-10-23_1%d0000', 3 + s)); mkdir(f);
        D.trimmed = false;
        D = normalize(D, f);
        D.closedDelta = 18; D.motionThr = 0.18;
        saveTrimmed(D, [100 175], f);
        T = loadTrimmed(f);
        assert(~any(T.timeLog >= 100 & T.timeLog <= 175) && ~any(T.heartTimeLog >= 100 & T.heartTimeLog <= 175) ...
               && ~any(T.tempTimeLog >= 100 & T.tempTimeLog <= 175) && ~any(T.beatTimes >= 100 & T.beatTimes <= 175), ...
               'cut applied to every stream');
        assert(numel(T.blinkTimestamps) == 21 && numel(T.centBlinkTimestamps) == 7, 'blinks cut');
        assert(abs(seconds(T.t0 - D.t0)) < 1e-3, 'trimmed data keeps the session clock');
        assert(abs(T.recMin - 3.75) < 0.05, sprintf('kept %.2f min', T.recMin));
        assert(isnan(T.minBinCounts(3)) && T.minBinCounts(1) == 5, 'per-minute bins rebuilt; cut minute = NaN');
        assert(isfile(fullfile(f, 'trimmed_cuts.csv')) && isequal(readCuts(f), [100 175]), 'cut list saved');
    end
    F = loadTrimmed(fullfile(root, 'FrankensteinData'));
    assert(isFrankenstein(fullfile(root, 'FrankensteinData')), 'Frankenstein marker');
    assert(abs(seconds(F.t0 - datetime(2026, 10, 23, 14, 0, 0))) < 1e-3, 'stitched clock starts at first session');
    assert(numel(F.blinkTimestamps) == 42 && any(F.timeLog > 3600), 'both sessions stitched on one clock');
    fprintf('Plot_Captured_Eye_Data_V6_Win_to_Mac self-test: all passed.\n');
end

% =========================================================================
% GazeMetrics -- embedded copy of Other/GazeMetrics.m (keep the two identical)
% =========================================================================
function G = GazeMetrics(t, x, y)
    % Gaze shifts (pupil jump >= SHIFT_PX between usable frames), fixations
    % (steady time between two shifts, no blink/gap) and movement (px/s).
    % See Other/GazeMetrics.m for the full description.
    SHIFT_PX = 3;      % px in the 80x80 eye box, ~5x the pupil finder's jitter
    MAX_GAP  = 0.35;   % s between frames; longer = continuity broken

    t = t(:).'; x = x(:).'; y = y(:).';
    dt = diff(t); d = hypot(diff(x), diff(y));
    use  = ~isnan(d) & dt > 0 & dt <= MAX_GAP;
    jump = use & d >= SHIFT_PX;
    onset = find(jump & ~[false, jump(1:end-1)]);   % pair index i = frames i -> i+1

    G.move = d(use) ./ dt(use);
    G.trackedSec = sum(dt(use));
    G.shiftT = t(onset + 1);
    G.fixT = zeros(1, 0); G.fixDur = zeros(1, 0);
    for j = 1:numel(onset) - 1
        e = onset(j);
        while jump(e + 1), e = e + 1; end           % last pair of this shift
        o = onset(j + 1);
        if t(o) > t(e + 1) && all(use(e + 1:o - 1))
            G.fixT(end+1) = t(e + 1); G.fixDur(end+1) = t(o) - t(e + 1); %#ok<AGROW>
        end
    end

    G.rate = NaN; G.fix = NaN; G.moveMean = NaN;
    if G.trackedSec >= 10
        G.rate = numel(G.shiftT) / (G.trackedSec / 60);
        G.moveMean = mean(G.move);
    end
    if ~isempty(G.fixDur), G.fix = median(G.fixDur); end
end
