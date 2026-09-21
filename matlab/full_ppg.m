function full_ppg(port)
% FULL_PPG  Live view for the MAX30102 heart-rate/SpO2 + MLX90614 temperature
% standalone sensor build. Firmware: firmware/projects/full_ppg/ -- a native
% ESP-IDF project (idf.py build/flash/monitor), not an Arduino sketch. The
% MAX30102/MLX90614 registers are driven directly per their datasheets
% (main/max30102.c, main/mlx90614.c); no Arduino library involved.
%
% Usage:
%   full_ppg                       % uses the default port below
%   full_ppg("/dev/cu.usbmodemXXXX")
%
% PROTOCOL: plain text lines over the chip's native USB port (no camera on
% this build, so none of the binary framing the glasses project needed for
% its bandwidth-starved link):
%   W,<filteredIR>,<filteredRed>                          ~25 lines/s
%   R,<bpm>,<hrValid>,<spo2>,<spo2Valid>,<tempObj>,<tempAmb>   ~1 line/s
% This protocol is unchanged from the firmware's earlier Arduino version --
% only the firmware's language/build system changed, not what it sends.
%
% readline() is called once per line here rather than the glasses project's
% bulk-read-and-frame-in-memory approach. That complexity existed ONLY
% because a camera was saturating the link (~96 kB/s) against MATLAB
% serialport's measured ~60 transactions/s ceiling. Without a camera, the
% combined ~26 lines/s here sits well under that ceiling, so the simple,
% idiomatic per-line read is the correct tool -- see memory:
% glasses-v5-working-config for the full story of why that ceiling matters.
%
% SpO2 CAVEAT: the firmware computes SpO2 from the standard ratio-of-ratios
% method but maps it to a percentage with the widely-used EMPIRICAL formula
% (SpO2 = 110 - 25*R) rather than a lab calibration curve. Treat it as a
% trend indicator, not a medical-grade reading.

    if nargin < 1
        port = "/dev/cu.usbmodem101";   % CHANGE to match your board; run
                                        % serialportlist to find it
    end

    s = serialport(port, 115200, "Timeout", 2);
    configureTerminator(s, "LF");
    flush(s);
    cleanupObj = onCleanup(@() delete(s));

    % --- window / display parameters ---
    WAVE_WIN   = 250;    % ~10 s at the firmware's ~25 lines/s waveform rate
    STALE_SEC  = 3.0;    % no R-line for this long -> readouts go blank

    waveIR   = nan(1, WAVE_WIN);
    waveRed  = nan(1, WAVE_WIN);
    waveX    = 1:WAVE_WIN;

    bpm = NaN; hrValid = false; spo2 = NaN; spo2Valid = false;
    tempObj = NaN; tempAmb = NaN;
    lastResultTic = tic;
    haveResult = false;

    % --- figure ---
    fig = figure('Name', 'full_ppg — MAX30102 + MLX90614 live view', ...
                 'NumberTitle', 'off', 'Color', [0.08 0.08 0.08], ...
                 'Position', [80 80 1000 640]);

    axWave = axes(fig, 'Position', [0.07 0.34 0.88 0.58]);
    set(axWave, 'Color', [0.13 0.13 0.13], 'XColor', 'w', 'YColor', 'w', ...
        'GridColor', [0.35 0.35 0.35], 'GridAlpha', 0.6);
    grid(axWave, 'on'); hold(axWave, 'on');
    hIR  = plot(axWave, waveX, waveIR,  '-', 'Color', [0.30 0.65 1.00], ...
                'LineWidth', 1.3, 'DisplayName', 'IR (filtered)');
    hRed = plot(axWave, waveX, waveRed, '-', 'Color', [1.00 0.35 0.30], ...
                'LineWidth', 1.0, 'DisplayName', 'Red (filtered)');
    ylim(axWave, [-300 300]);
    xlim(axWave, [1 WAVE_WIN]);
    xlabel(axWave, sprintf('Sample index (most recent %d = ~%.0f s)', ...
           WAVE_WIN, WAVE_WIN/25), 'Color', 'w');
    ylabel(axWave, 'DC-blocked ADC counts', 'Color', 'w');
    title(axWave, 'PPG Waveform — waiting for data', 'Color', 'w', ...
          'FontWeight', 'bold');
    legend(axWave, 'Location', 'northeast', 'TextColor', 'w', ...
           'Color', [0.10 0.10 0.10], 'EdgeColor', [0.45 0.45 0.45]);

    dark = @(p) uicontrol('Style', 'text', 'Units', 'normalized', ...
        'BackgroundColor', [0.1 0.1 0.1], 'ForegroundColor', [0.6 0.6 0.6], ...
        'FontSize', 14, 'FontWeight', 'bold', 'HorizontalAlignment', 'center', ...
        'Position', p);
    hBpmBig  = dark([0.02 0.04 0.30 0.20]);
    hSpo2Big = dark([0.35 0.04 0.30 0.20]);
    hTempBig = dark([0.68 0.04 0.30 0.20]);
    set(hBpmBig,  'String', {'BPM: --- ', '(no finger)'});
    set(hSpo2Big, 'String', {'SpO2: ---', '(no finger)'});
    set(hTempBig, 'String', {'TEMP: ---', 'waiting...'});

    fprintf('full_ppg: connected on %s. Close the figure window to stop.\n', port);

    % robustScale/fitAxis: verbatim from the glasses project (already tuned
    % there -- 95th percentile + headroom avoids a finger-arrival step
    % blowing up the axis; hysteresis stops the trace breathing every frame).
    while ishandle(fig)
        line = "";
        try
            if s.NumBytesAvailable > 0
                line = readline(s);
            end
        catch
            break;   % port vanished (unplugged, etc.)
        end

        if strlength(line) > 0
            parts = strsplit(line, ',');
            tag = parts{1};

            if tag == "W" && numel(parts) >= 3
                irV  = str2double(parts{2});
                redV = str2double(parts{3});
                if ~isnan(irV) && ~isnan(redV)
                    waveIR  = [waveIR(2:end),  irV];
                    waveRed = [waveRed(2:end), redV];
                    set(hIR,  'YData', waveIR);
                    set(hRed, 'YData', waveRed);
                    want = max(robustScale(waveIR), robustScale(waveRed));
                    L = fitAxis(ylim(axWave), want);
                    if ~isequal(L, ylim(axWave)), ylim(axWave, L); end
                end

            elseif tag == "R" && numel(parts) >= 7
                bpm       = str2double(parts{2});
                hrValid   = str2double(parts{3}) ~= 0;
                spo2      = str2double(parts{4});
                spo2Valid = str2double(parts{5}) ~= 0;
                tempObj   = str2double(parts{6});
                tempAmb   = str2double(parts{7});
                lastResultTic = tic;
                haveResult = true;

            elseif tag == "E"
                fprintf('[FIRMWARE] %s\n', line);
            end
        end

        % --- readouts, refreshed every pass but STALE-gated on the result age
        stale = ~haveResult || toc(lastResultTic) > STALE_SEC;
        if stale
            set(hBpmBig,  'String', {'BPM: ---', '(no finger)'}, ...
                'ForegroundColor', [0.6 0.6 0.6]);
            set(hSpo2Big, 'String', {'SpO2: ---', '(no finger)'}, ...
                'ForegroundColor', [0.6 0.6 0.6]);
            title(axWave, 'PPG Waveform — no recent data', 'Color', ...
                  [1 0.35 0.35], 'FontWeight', 'bold');
        else
            if hrValid
                set(hBpmBig, 'String', {sprintf('BPM: %.0f', bpm), ''}, ...
                    'ForegroundColor', [1 1 0.3]);
            else
                set(hBpmBig, 'String', {'BPM: ---', '(low confidence)'}, ...
                    'ForegroundColor', [1 0.6 0.2]);
            end
            if spo2Valid
                set(hSpo2Big, 'String', {sprintf('SpO2: %.0f%%', spo2), ''}, ...
                    'ForegroundColor', [0.4 0.85 1]);
            else
                set(hSpo2Big, 'String', {'SpO2: ---', '(low confidence)'}, ...
                    'ForegroundColor', [1 0.6 0.2]);
            end
            title(axWave, 'PPG Waveform', 'Color', 'w', 'FontWeight', 'bold');
        end

        if haveResult && ~isnan(tempObj)
            set(hTempBig, 'String', ...
                {sprintf('OBJ: %.2f \xB0C', tempObj), ...
                 sprintf('AMB: %.2f \xB0C', tempAmb)}, ...
                'ForegroundColor', [1.00 0.55 0.15]);
        end

        drawnow limitrate;
    end

    fprintf('full_ppg: stopped.\n');
end

function m = robustScale(v)
% Half-height for a y-axis that ignores step transients but does not clip
% the pulse. Verbatim from the glasses project (Simplified_eye_V6.m),
% already tuned there: 95th percentile + 1.3 headroom, floor of 25 so an
% empty/no-finger window doesn't zoom into its own noise.
    v = v(~isnan(v));
    if isempty(v), m = 25; return; end
    PCT = 0.95; HEADROOM = 1.3;
    a = sort(abs(v));
    p = a(max(1, ceil(PCT * numel(a))));
    m = max(25, p * HEADROOM);
end

function lim = fitAxis(cur, want)
% Hysteresis band -- leave the axis alone while it's within 25% of what the
% data wants, so the scale doesn't breathe on every redraw.
    if want > cur(2) || want < 0.75 * cur(2)
        lim = [-1 1] * want;
    else
        lim = cur;
    end
end
