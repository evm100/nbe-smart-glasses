
function NOT_Simplified_eye_V7_win_to_mac(mode)
    % =========================================================================
    % SIMPLIFIED_EYE_V5 — V4 + live heart waveform + non-contact temperature
    %
    % Based on SIMPLIFIED_EYE_V4. The eye-tracking, blink-detection and LDR
    % light-state logic are UNCHANGED — this is purely additive. What is new:
    %   - MAX30102 PPG waveform streamed on the same link, plotted live across
    %     the bottom of the window (filtered IR left axis, derivative right).
    %     Replaces the standalone Heart_Waveform_1.m.
    %   - MLX90614 ambient + object temperature, shown as a large readout.
    %     Replaces the standalone MLX90614_LiveTemp.m.
    %   - Both channels are logged and saved alongside the blink data, so a
    %     session is one coherent record instead of three separate captures.
    %
    % -------------------------------------------------------------------------
    % SUBSYSTEM INDEPENDENCE — every channel stands alone
    % 
    %   Camera, LDR, IR LEDs, heart and temperature are fully independent. Any 
    %   of them can be absent, unplugged or broken without affecting the others.
    %   Three things make that true on this side:
    %
    %     1. Sync accepts ANY legal header, not just a camera frame. V4 waited
    %        for [255 0 255 0], so a board with no camera never started a
    %        session even while its sensors streamed perfectly.
    %     2. The sensor readouts refresh on their OWN cadence, not inside the
    %        frame path. In V4 the LDR, temperature and heart displays were
    %        drawn only after a frame arrived, so no camera meant frozen
    %        readouts. That was the worst of the coupling.
    %     3. The 1 Hz status packet reports which subsystems the firmware
    %        actually has, so "absent" and "silent" are distinguishable, and the
    %        link always carries something to sync on.
    %
    %   The firmware never halts either — a missing camera disables only frames.
    %
    % -------------------------------------------------------------------------
    % SERIAL PROTOCOL (2 Mbaud) — must match Simplified_eye_V6_code.ino
    %
    %   CAMERA FRAME  [255 0 255 0] + 6400 bytes (80x80 uint8, row-major)
    %   LDR READING   [255 0 254 0] + 2 bytes  uint16 LE
    %   TEMPERATURE   [255 0 253 0] + 4 bytes  int16 LE ambient, int16 LE object,
    %                                          both in HUNDREDTHS of a degree C
    %   HEART BATCH   [255 0 252 0] + 1 byte N, then N x 4 bytes:
    %                                          int16 LE filtered, int16 LE derivative
    %   STATUS        [255 0 251 0] + 1 byte bitmask, 1 Hz:
    %                   1 camera  2 LDR  4 heart  8 temp  16 IR LED on
    %
    %   Only byte 3 differs. Read 4 header bytes, switch on byte 3, read the
    %   payload that header implies. The ESP32 writes each packet in one
    %   uninterrupted burst, so a header is always followed by exactly its own
    %   payload.
    %
    %   WHY HEART SAMPLES ARRIVE BATCHED: the PPG runs at 100 Hz. One packet per
    %   sample would be 100 headers/s to parse on top of ~25 frames/s. The
    %   firmware drains its FIFO once per loop and sends one packet, giving
    %   ~3-4 samples per packet for 4 extra bytes.
    %
    %   HEART POLARITY: both values are NEGATED here before plotting, exactly as
    %   Heart_Waveform_1.m and the original Python did. In a PPG the IR reading
    %   DROPS as blood volume rises, so the raw AC signal is upside down; the
    %   negation puts the systolic peak the right way up. The firmware does not
    %   pre-invert, so this must not be removed or it will be flipped twice.
    %
    %   MATLAB -> ESP32 is unchanged: 0xF1 = IR LEDs ON, 0xF0 = OFF.
    % -------------------------------------------------------------------------
    %
    % THRESHOLD SETS
    %   (Since 2026-10-28 frames are lighting-normalised first, noEyeThresh is
    %    no longer used and darkPixelThresh is 80 in both sets -- see header.)
    %   NORMAL  (IR LEDs OFF): noEyeThresh=160, darkPixelThresh=80,
    %                           closedDeltaPx=18, openMarginPx=9, peakThreshold=0.18
    %   IR/DARK (IR LEDs ON):  noEyeThresh=255, darkPixelThresh=40,
    %                           closedDeltaPx=13.6, openMarginPx=8.0, peakThreshold=0.18
    %
    %   NOTE on darkPixelThresh=40: V3's docstring claimed 147 but V3's CODE
    %   ran 40. 40 is carried forward here because it is what V3 actually
    %   executed and therefore what the IR sessions were recorded with. The
    %   147 alternative is left commented at the swap site — if IR tracking
    %   looks wrong, that is the first knob to try.
    %
    %   WHICH SET IS ACTIVE is decided by useDarkThresholds, set by EITHER the
    %   manual IR button OR an LDR light-state transition. Last event wins.
    %
    % AUTOMATIC IR LED SWITCHING
    %   An LDR transition into DARK turns the IR LEDs ON and installs the
    %   IR/DARK thresholds; a transition into INDOOR or OUTSIDE turns them OFF
    %   and installs the NORMAL thresholds. LED bytes still go out over the
    %   same link (0xF1 / 0xF0) — the ESP32 firmware is unchanged and does no
    %   switching of its own.
    %
    %   The manual button still works and takes effect immediately. Because
    %   the LDR only acts on a state CHANGE, a manual override survives until
    %   the ambient light next crosses a boundary, at which point automatic
    %   control resumes. Both paths go through applyIRMode() so the LEDs and
    %   the button's appearance can never disagree.
    %
    %   OSCILLATION GUARD: the GL5528 has some residual sensitivity at IR
    %   wavelengths, so switching the IR LEDs on can raise the LDR's own
    %   reading. If the LDR sits close enough to the emitters for that bump
    %   to clear the DARK hysteresis band, the loop would chatter
    %   (dark -> LEDs on -> reads brighter -> LEDs off -> ...). irAutoLockout
    %   below blocks automatic LED changes for a few seconds after each one.
    %
    % TRACKING METHOD
    %   The dark-pixel centroid (red cross) is rock-stable on the pupil when
    %   the eye is open and jumps to a new stable position when the eye is
    %   held closed. We use distance from a locked open-eye baseline as the
    %   open/closed indicator. The motion signal (frame diff) is used purely
    %   to count blinks AND to confirm state transitions.
    %
    % STATE MACHINE  (per completed motion peak)
    %   OPEN  + centroid moved away from baseline > closedDeltaPx -> CLOSED, start timer
    %   CLOSED + centroid back within openMarginPx of baseline    -> OPEN, record duration
    %   Short blink (peak fires, centroid already returned)       -> counted, ~0 dur
    %
    % BLINK COUNTING - simple motion peak detector (matches old version,
    %   no cross-validation rejection).
    % =========================================================================
    % WIN_TO_MAC COPY (2026-10-28) -- Simplified_eye_V6 for recording on the
    % Mac and reviewing with Plot_Captured_Eye_Data_V6_Win_to_Mac. Changes:
    %   - Each session saves into its OWN folder:
    %       ~/Desktop/MyDataFolder/session_<yyyy-mm-dd_HHMMSS>/
    %     (V6 put every session flat in Desktop/Resaerch/MyDataFolder). The
    %     plotter's trimming and FrankensteinData work per session folder.
    %   - sessionStartWall (clock time of time_s = 0) is saved in the .mat, so
    %     the plots show real clock time. The folder name alone is off by the
    %     up-to-30 s sync wait, which happens after the session clock starts.
    %   - nResync was never initialised: the first mid-session resync crashed
    %     the loop and ended the session.
    %   - NO 20-MINUTE LIMIT. START / STOP buttons instead: START counts down
    %     3-2-1 and then records (time_s = 0 at that moment); STOP asks for
    %     confirmation, then ends the run and saves. Closing the window still
    %     ends and saves too, as before. The session folder is named after the
    %     moment START was pressed.
    %   - LDR CALIBRATION button next to the LDR readout. Measures the room for
    %     3 s, then (after you cover the sensor and click "Measure dark") the
    %     dark level for 3 s, and puts the DARK switch point halfway between,
    %     +/- 10% hysteresis. Fixes "dimmer room = DARK = IR LEDs on". IR LEDs
    %     are switched off first so they cannot light the sensor. Works before
    %     START and while recording (the ~6 s it takes is a gap in the data).
    %     Every calibration is saved in the .mat as ldrCalLog.
    %   - LIGHTING NORMALISATION (2026-10-28). The camera's exposure is fixed in
    %     firmware, and the tracker used FIXED cutoffs on the raw image ("no eye"
    %     if mean > 160, pupil = pixels < 80). Real worn frames already average
    %     150-177, so a bit more light paused tracking as NO EYE and a bit less
    %     turned the whole image into "pupil". Each frame is now mapped onto the
    %     brightness range of a typical well-lit frame (2nd..98th percentile ->
    %     60..200) before ANY tracking, so the tuned cutoffs mean the same thing
    %     in every lighting. Measured on 7 real frames with brightness scaled
    %     x0.3 to x1.4: tracked within 4 px in 48/49 cases (old: 14/49).
    %     "No eye" is now: image washed out (>= half the pixels at 250+) or no
    %     contrast left (raw range < 15, too dark for the camera -> IR LEDs).
    %   - BLINK DETECTION FIXES (2026-10-28), measured on the 10-23 sessions:
    %     * Head-move filter split the image at the FIXED middle row. It threw
    %       away 19-42% of motion spikes, rising over a session (9% -> 43% in
    %       16:31) as the glasses slipped and the eye moved in the frame. The
    %       split now follows the eye: it is the rolling baseline row.
    %     * Closure is measured along the eye's own CLOSING DIRECTION, learnt
    %       from motion-detected blinks (default straight down, kept within
    %       75 deg of down). A camera that sees the lid close diagonally made
    %       the centroid go sideways, so its downward part never crossed the
    %       closed threshold. Green now moves vertically only and shows that
    %       closing-direction deviation; RED is the gaze (both axes logged).
    %     * Per-frame head-move flag (suppressLog) and gaze X (centXlog) are
    %       saved, so the plotter's re-detection applies the same filter.
    %   - PAUSE / RESUME button. While paused nothing is recorded; each pause
    %     is saved (pauseLog, seconds since start) and the plotter labels it
    %     "PAUSE n" with its clock time and duration. Tracking re-seeds on
    %     resume (the glasses were probably taken off).
    %
    % V7 (NOT_Simplified_eye_V7_*, needs NOT_Simplified_eye_V7_code.ino):
    %   - Frame rate set from here (FRAME_MS) instead of the firmware's fixed
    %     8 fps; every frame is tracked and saved, the live plots redraw at
    %     ~10 Hz (UI_PERIOD) so drawing cannot hold the tracker back.
    %   - AUTO EXPOSURE (closed loop, logged): the camera exposure/gain are
    %     nudged <=10% per second to keep the eye image's highlights near
    %     EXPO_TARGET. Dim rooms gave frames averaging ~50/255, and that noise
    %     was the main cause of jumpy tracking. Each change is logged in
    %     exposureLog [t aec gain] and blocks blink onsets for 2 frames.
    % =========================================================================
    %   TUNABLES
    %     closedDeltaPx           -> centroid must move this many px from
    %                               baseline to register as CLOSED (default 18)
    %     openMarginPx            -> return within this many px = OPEN (default 9)
    %     peakThreshold           -> motion signal threshold for blink (0.18)
    %     minBlinkGap             -> min seconds between counted blinks (0.2)
    %     headMoveUpperFracThresh -> head-move suppression (0.50)
    %
    %   GAZE (per minute; Other/GazeMetrics.m, shared with the plotter)
    %     The PUPIL is the darkest round spot (9x9 mean, so thin lashes and
    %     brow lines wash out), refined to the dark pixels within 8 px. Its
    %     position is logged only while the eye is open, no head move is
    %     flagged and a pupil is found (pupilXlog/pupilYlog, NaN otherwise).
    %     Gaze shifts / min  pupil jumped >= 3 px (any direction) between
    %                        frames, per minute of tracked time
    %     Fixation (s)       median steady time between two shifts
    %     Gaze movement      mean pupil speed, px/s (per second, so frame
    %                        rate does not change it)
    %     RED + on the eye image = the pupil used for gaze.
    %
    %   KEYS:  Space -> save now      R -> re-init baseline
    %
    %   NOT_Simplified_eye_V7_win_to_mac('test')  -> offline self-check of the protocol,
    %                                 light hysteresis, IR arbitration and the
    %                                 new int16/heart-batch decoders.
    %                                 No hardware, no serial port.
    % =========================================================================


    % PerMinutePanels.m lives in the Other/ subfolder. MATLAB searches the
    % current folder and the path but NOT subfolders, so once that helper was
    % filed away the first call to it failed with "Unrecognized function or
    % variable 'PerMinutePanels'". Resolved from THIS file's own location
    % rather than pwd, so it works no matter which folder MATLAB is sitting in.
    helperDir = fullfile(fileparts(mfilename('fullpath')), 'Other');
    if isfolder(helperDir) && ~any(strcmp(helperDir, strsplit(path, pathsep)))
        addpath(helperDir);
    end

    if nargin > 0 && strcmp(mode, 'test'), selfTest(); return; end

    % --- 1. CONNECTION ---
    port = "/dev/cu.usbmodem101";
    baud = 2000000;

    % RERUN FIX ("it only runs again after unplugging the glasses"):
    %  1. A run stopped with Ctrl+C / an error left its serialport object
    %     alive, holding the port. Close every leftover one first, and close
    %     ours on ANY exit (onCleanup runs on Ctrl+C too).
    %  2. The port can come back under another name after a replug, so it is
    %     looked up rather than assumed.
    %  3. A board that does not stream within 8 s is reset over USB (see SYNC).
    try, delete(serialportfind); catch, end
    port = glassesPort(port);
    % V7: ALWAYS reset on connect. Measured 2026-10-28 on the real board: a
    % plain MATLAB open got NO data after any earlier MATLAB session had
    % used the port (an outside program read it fine at the same moment),
    % while "DTR low, RTS pulse, reopen" worked 4/4 with the first frame
    % ~3.5 s later. This is the actual "only runs again after unplugging"
    % fix; it also gives every run a freshly booted board.
    s = serialport(port, baud);
    s = resetBoard(s, port, baud);
    if isempty(s), return; end
    portCleanup = onCleanup(@() closeAllPorts()); %#ok<NASGU>

    % Force the IR LEDs OFF before anything else.
    %
    % The ESP32 holds its LED pin state until reset or the next command, and
    % V4 used to neither initialise nor clean it up. So a session inherited
    % whatever the PREVIOUS session left behind — which is why the same room
    % read ~700 on one run and ~70 on the next with nothing touched. Every run
    % now starts from the same known state.
    write(s, uint8(0xF0), 'uint8');
    pause(0.3);                       % let the firmware's moving average settle
    flush(s);

    roiSize = 80; numPixels = roiSize^2;

    % --- EXPERIMENT SETUP ---
    % No fixed duration: the run lasts from START to STOP. sessionTag and the
    % save folder are created when START is pressed (see WAIT FOR START), so a
    % window opened and closed without recording leaves no empty folder.

    snapshotTime  = 10 * 60;
    snapshotTaken = false;

    % --- 2. FIGURE SETUP ---
    % V5 adds a second row for the heart waveform, so the window is taller and
    % the three V4 axes move into the top row of a 2x3 grid.
    fig = figure('Name', 'Blink Detector V6 — Eye + LDR + Heart + BPM + Temp', 'NumberTitle', 'off', ...
                 'KeyPressFcn', @keyPressCallback, 'Color', [0.1 0.1 0.1], ...
                 'Position', [80 60 1600 950], ...
                 'CloseRequestFcn', @(src,evt) closeRequestHandler(src));

    ax = subplot(2, 3, 1);
    hImg = imshow(zeros(roiSize, roiSize), [0 255]);
    colormap(gray); hold on;
    title(ax, 'Live Eye Feed — 80x80 px ROI', 'Color', [0.85 0.85 0.85], 'FontSize', 9);
    hStateText = text(5, 8,  'WAITING...', 'Color', 'yellow',    'FontSize', 9, 'FontWeight', 'bold');
    pE = get(ax, 'Position');
    uicontrol('Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [pE(1), pE(2) - 0.045, 0.48 * pE(3), 0.035], ...
        'String', 'Reset green marker', 'FontSize', 10, 'FontWeight', 'bold', ...
        'BackgroundColor', [0.2 0.55 0.2], 'ForegroundColor', 'w', ...
        'TooltipString', 'Green stuck? Re-learns the open-eye baseline (~4 s). Same as the R key.', ...
        'Callback', @(~, ~) requestMarkerReset());
    uicontrol('Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [pE(1) + 0.52 * pE(3), pE(2) - 0.045, 0.48 * pE(3), 0.035], ...
        'String', 'Eye calibrate', 'FontSize', 10, 'FontWeight', 'bold', ...
        'BackgroundColor', [0.35 0.35 0.75], 'ForegroundColor', 'w', ...
        'TooltipString', ['3 s eyes OPEN, beep, 3 s eyes CLOSED, double beep. ' ...
                          'Lets long closures be timed to the real re-opening.'], ...
        'Callback', @(~, ~) requestEyeCal());
    hWearText  = text(5, 16, '',           'Color', 'white',     'FontSize', 8);
    hAlphaText = text(5, 24, '',           'Color', [1 0.6 0],   'FontSize', 8);
    hHeadText  = text(5, 32, '',           'Color', [1 0.55 0],  'FontSize', 8, 'FontWeight', 'bold');
    hLightText = text(5, 40, 'LIGHT: --- (no LDR)', 'Color', [0.6 0.6 0.6], 'FontSize', 8, 'FontWeight', 'bold');
    % V5: camera liveness. A dead camera would otherwise show as a frozen last
    % frame, which looks identical to a very still eye.
    hCamText   = text(5, 48, 'NO CAMERA — other sensors still live', ...
                      'Color', [1 0.35 0.35], 'FontSize', 8, 'FontWeight', 'bold');

    % PERSISTENT OVERLAY HANDLES, created ONCE.
    %
    % These three used to be plot() calls inside the frame loop, preceded by
    % delete(findall(ax,'Tag','overlay')). That is the classic MATLAB real-time
    % mistake: findall walks the entire object tree, and plot() builds new
    % graphics objects, every single frame. MathWorks' own guidance is to reuse
    % one object and update XData/YData — reported as >10x faster — and to keep
    % the input loop doing the minimum work so the serial stream keeps flowing.
    % Here that work was starving the heart packets outright.
    hCentRaw  = plot(ax, nan, nan, 'r+', 'MarkerSize', 10, 'LineWidth', 1.5);
    hIrisCircle = plot(ax, nan, nan, 'g-', 'LineWidth', 1.5);    % V7: the tracked pupil/iris
    hBaseLine = plot(ax, nan, nan, 'y--', 'LineWidth', 1);

    text(1, roiSize-14, 'RED +    = raw pupil centroid',           'Color', 'red',    'FontSize', 6, 'Tag', 'legend_static');
    text(1, roiSize-8,  'GREEN +  = amplified centroid (gain x)',  'Color', 'green',  'FontSize', 6, 'Tag', 'legend_static');
    text(1, roiSize-2,  'YELLOW - = rolling baseline (drifts)',    'Color', 'yellow', 'FontSize', 6, 'Tag', 'legend_static');

    axBlink = subplot(2, 3, 2);
    set(axBlink, 'Color', [0.15 0.15 0.15], 'XColor', 'w', 'YColor', 'w', ...
        'GridColor', [0.35 0.35 0.35], 'GridAlpha', 0.6, 'MinorGridAlpha', 0.3);
    grid(axBlink, 'on');
    hold(axBlink, 'on');
    xlabel(axBlink, 'Time (s)', 'Color', 'w', 'FontSize', 9);
    ylabel(axBlink, 'Frame Difference (normalized 0-1)', 'Color', 'w', 'FontSize', 8);
    title(axBlink, 'Blink Detection (motion peaks)', 'Color', 'w', 'FontSize', 10, 'FontWeight', 'bold');
    ylim(axBlink, [-0.1 1.2]); xlim(axBlink, [0 60]);
    hDiffLine    = plot(axBlink, nan, nan, 'm-', 'LineWidth', 1,   'DisplayName', 'Motion signal');
    hBlinkMarker = plot(axBlink, nan, nan, 'g-', 'LineWidth', 2,   'DisplayName', 'Blink detected');
    hThreshLine  = yline(axBlink, 0.18, 'w:',   'LineWidth', 1.2, 'DisplayName', 'Detection threshold (0.18)');
    legend(axBlink, [hDiffLine, hBlinkMarker, hThreshLine], ...
        'Location', 'northwest', 'TextColor', 'w', 'FontSize', 7, ...
        'Color', [0.12 0.12 0.12], 'EdgeColor', [0.45 0.45 0.45]);
    % Blink markers as ONE line, extended with NaN-separated segments. Every
    % blink used to create a fresh plot() object that was never removed, so a
    % session accumulated hundreds of permanent objects, all redrawn on every
    % frame — the reason performance degraded the longer it ran. Created here,
    % after axBlink exists.
    hBlinkMarks = plot(axBlink, nan, nan, 'g-', 'LineWidth', 1.5, ...
                       'HandleVisibility', 'off');

    hMotionCount = text(axBlink, 0.98, 0.96, 'MOTION blinks: 0', 'Units', 'normalized', ...
        'HorizontalAlignment', 'right', 'VerticalAlignment', 'top', ...
        'Color', [0.4 1 0.4], 'FontSize', 11, 'FontWeight', 'bold');

    axCent = subplot(2, 3, 3);
    set(axCent, 'Color', [0.15 0.15 0.15], 'XColor', 'w', 'YColor', 'w', ...
        'GridColor', [0.35 0.35 0.35], 'GridAlpha', 0.6);
    grid(axCent, 'on');
    hold(axCent, 'on');
    xlabel(axCent, 'Time (s)', 'Color', 'w', 'FontSize', 9);
    ylabel(axCent, 'Eye closure (%)', 'Color', 'w', 'FontSize', 8);
    title(axCent, 'Eye Closure (0 = open)', 'Color', 'w', 'FontSize', 10, 'FontWeight', 'bold');
    ylim(axCent, [-30 100]); xlim(axCent, [0 60]);
    hCentAmpLog   = plot(axCent, nan, nan, 'g-',  'LineWidth', 1.2, 'DisplayName', 'Eye closure % (pupil/iris faded vs open)');
    hBasePlot     = plot(axCent, nan, nan, 'y--', 'LineWidth', 1.5, 'DisplayName', 'Open eye (0 %)');
    hClosedHi     = plot(axCent, nan, nan, '-',   'LineWidth', 1.4, 'Color', [1.00 0.45 0.10], ...
                          'DisplayName', 'Closed threshold (slider)');
    hClosedLo     = plot(axCent, nan, nan, '-',   'LineWidth', 0.8, 'Color', [1.00 0.45 0.10], ...
                          'HandleVisibility', 'off', 'Visible', 'off');
    hCentBlinkLegend = plot(axCent, nan, nan, 'c-', 'LineWidth', 1.5, 'DisplayName', 'Centroid blink (threshold cross)');
    legend(axCent, [hCentAmpLog, hBasePlot, hClosedHi, hCentBlinkLegend], ...
        'Location', 'northwest', 'TextColor', 'w', 'FontSize', 7, ...
        'Color', [0.12 0.12 0.12], 'EdgeColor', [0.45 0.45 0.45]);
    % Same treatment for the centroid closure markers. Created after axCent.
    hCentMarks = plot(axCent, nan, nan, 'c-', 'LineWidth', 1.0, ...
                      'HandleVisibility', 'off');

    hCentCount = text(axCent, 0.98, 0.96, 'CENTROID blinks: 0', 'Units', 'normalized', ...
        'HorizontalAlignment', 'right', 'VerticalAlignment', 'top', ...
        'Color', 'c', 'FontSize', 11, 'FontWeight', 'bold');

    % --- CLOSED-THRESHOLD SLIDER (live) ---
    % Moves the orange closed line. Slider DOWN = line lower = a deeper closure
    % is needed (same direction as the plotter's TRIM window). Applies from the
    % next frame; every change is logged in closedLog [t px].
    pC = get(axCent, 'Position');
    hClosedSld = uicontrol('Style', 'slider', 'Units', 'normalized', ...
        'Position', [pC(1) + pC(3) + 0.012, pC(2), 0.012, pC(4)], ...
        'Min', 10, 'Max', 70, 'Value', 30, 'SliderStep', [1 5] / 60, ...
        'TooltipString', 'Closed threshold (orange line), % closure: drag up = needs a deeper closure', ...
        'Callback', @(src, ~) setClosedPx(src));
    addlistener(hClosedSld, 'ContinuousValueChange', @(src, ~) setClosedPx(src));
    hClosedSldLbl = uicontrol('Style', 'text', 'Units', 'normalized', ...
        'Position', [pC(1) + pC(3) - 0.004, pC(2) + pC(4) + 0.004, 0.045, 0.02], ...
        'BackgroundColor', [0.1 0.1 0.1], 'ForegroundColor', [1.00 0.45 0.10], ...
        'FontSize', 9, 'FontWeight', 'bold', 'String', '30 %');

    % --- HEART WAVEFORM (NEW IN V5) — bottom row, full width ---
    % One axes with two y-scales rather than the two stacked subplots
    % Heart_Waveform_1.m used: the filtered trace lives on +/-1500 and the
    % derivative on +/-10000, so they cannot share one scale, but they DO need
    % to share an x-axis to be read against each other.
    % TWO STACKED AXES, not one with yyaxis. Overlaying a +/-1500 signal and a
    % +/-10000 derivative in the same box made the derivative read as a flat
    % smear across the middle of the trace. Heart_Waveform_1.m has always used
    % separate panels; this brings V5 into line.
    %
    % Positioned explicitly rather than via subplot so the top row
    % (subplot(2,3,1..3)) keeps its existing geometry untouched.
    axHeartSig = axes(fig, 'Position', [0.05 0.10 0.29 0.34]);
    set(axHeartSig, 'Color', [0.15 0.15 0.15], 'XColor', 'w', 'YColor', 'w', ...
        'GridColor', [0.35 0.35 0.35], 'GridAlpha', 0.6);
    grid(axHeartSig, 'on'); hold(axHeartSig, 'on');
    hHeartFilt = plot(axHeartSig, nan, nan, '-', 'Color', [0.30 0.65 1.00], ...
                      'LineWidth', 1.2);
    ylabel(axHeartSig, 'Filtered IR', 'Color', 'w', 'FontSize', 8);
    ylim(axHeartSig, [-330 330]);        % starting point; robustScale takes over
    xlabel(axHeartSig, 'Sample index (most recent 600 = ~6 s at 100 Hz)', ...
           'Color', 'w', 'FontSize', 9);
    title(axHeartSig, 'Heart Waveform (MAX30102) — waiting for data', ...
          'Color', 'w', 'FontSize', 10, 'FontWeight', 'bold');

    axHeartDeriv = axes(fig, 'Position', [0.375 0.10 0.29 0.34]);
    set(axHeartDeriv, 'Color', [0.15 0.15 0.15], 'XColor', 'w', 'YColor', 'w', ...
        'GridColor', [0.35 0.35 0.35], 'GridAlpha', 0.6);
    grid(axHeartDeriv, 'on'); hold(axHeartDeriv, 'on');
    hHeartDeriv = plot(axHeartDeriv, nan, nan, '-', 'Color', [1.00 0.35 0.30], ...
                       'LineWidth', 1.0);
    % Detected beats, drawn ON the derivative so a wrong lock is visible rather
    % than inferred from a BPM number that looks plausible. Persistent handle
    % updated via set(), never re-plotted — same reason as every other trace.
    hBeatMarks = plot(axHeartDeriv, nan, nan, 'v', 'Color', [1 1 0.3], ...
                      'MarkerFaceColor', [1 1 0.3], 'MarkerSize', 5, ...
                      'LineStyle', 'none');
    hBpmTxt = text(axHeartDeriv, 0.98, 0.94, '', 'Units', 'normalized', ...
                   'HorizontalAlignment', 'right', 'Color', [1 1 0.3], ...
                   'FontSize', 11, 'FontWeight', 'bold');
    ylabel(axHeartDeriv, 'd(IR)/dt', 'Color', 'w', 'FontSize', 8);
    ylim(axHeartDeriv, [-1000 1000]);
    xlabel(axHeartDeriv, 'Sample index (most recent 600 = ~6 s at 100 Hz)', ...
           'Color', 'w', 'FontSize', 9);
    title(axHeartDeriv, 'First derivative', 'Color', 'w', ...
          'FontSize', 10, 'FontWeight', 'bold');

    % --- TEMPERATURE OVER TIME (NEW IN V6) ---
    % The big readout above shows the instantaneous value; this shows the trend.
    % Skin temperature drifts slowly, which is exactly why a single number is a
    % poor way to watch it — a 0.4 C rise over ten minutes is invisible digit by
    % digit and obvious as a line.
    axTemp = axes(fig, 'Position', [0.70 0.10 0.28 0.34]);
    set(axTemp, 'Color', [0.15 0.15 0.15], 'XColor', 'w', 'YColor', 'w', ...
        'GridColor', [0.35 0.35 0.35], 'GridAlpha', 0.6);
    grid(axTemp, 'on'); hold(axTemp, 'on');
    hTempObjLine = plot(axTemp, nan, nan, '-', 'Color', [1 0.45 0.2], ...
                        'LineWidth', 1.4, 'DisplayName', 'Object (skin)');
    hTempAmbLine = plot(axTemp, nan, nan, '-', 'Color', [0.5 0.7 1], ...
                        'LineWidth', 1.0, 'DisplayName', 'Ambient');
    ylabel(axTemp, '\circC', 'Color', 'w', 'FontSize', 8);
    xlabel(axTemp, 'Time (s)', 'Color', 'w', 'FontSize', 9);
    title(axTemp, 'Temperature (MLX90614)', 'Color', 'w', ...
          'FontSize', 10, 'FontWeight', 'bold');
    legend(axTemp, 'Location', 'southeast', 'TextColor', 'w', 'FontSize', 7, ...
           'Color', [0.10 0.10 0.10], 'EdgeColor', [0.45 0.45 0.45]);

    hStats = uicontrol('Style', 'text', 'Units', 'normalized', ...
                       'Position', [0.01 0.005 0.98 0.032], ...
                       'BackgroundColor', [0.1 0.1 0.1], 'ForegroundColor', 'w', ...
                       'FontSize', 8, 'HorizontalAlignment', 'left');

    % --- START / STOP RECORDING (replace the 20-min countdown) ---
    hStartBtn = uicontrol('Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.165 0.952 0.05 0.042], 'String', 'START', ...
        'FontSize', 12, 'FontWeight', 'bold', 'BackgroundColor', [0.08 0.75 0.08], ...
        'ForegroundColor', 'w', 'Enable', 'off', 'Callback', @startRecording);   % on once the board is synced
    hPauseBtn = uicontrol('Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.217 0.952 0.05 0.042], 'String', 'PAUSE', ...
        'FontSize', 12, 'FontWeight', 'bold', 'BackgroundColor', [0.85 0.65 0.1], ...
        'ForegroundColor', 'w', 'Enable', 'off', 'Callback', @togglePause);
    hStopBtn = uicontrol('Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.269 0.952 0.05 0.042], 'String', 'STOP', ...
        'FontSize', 12, 'FontWeight', 'bold', 'BackgroundColor', [0.85 0.15 0.15], ...
        'ForegroundColor', 'w', 'Enable', 'off', 'Callback', @stopRecording);
    hTimer = uicontrol('Style', 'text', 'Units', 'normalized', ...
                       'Position', [0.321 0.950 0.062 0.046], ...
                       'BackgroundColor', [0.1 0.1 0.1], 'ForegroundColor', 'c', ...
                       'FontSize', 10, 'FontWeight', 'bold', ...
                       'HorizontalAlignment', 'center', ...
                       'String', 'Connecting...');

    % --- STIMULUS MARKER ---
    % Type what the stimulus is (e.g. "hand on ice"), press MARK at the moment
    % it starts. Shown as a labelled line on the live plots and in the plotter.
    hStimEdit = uicontrol('Style', 'edit', 'Units', 'normalized', ...
        'Position', [0.01 0.905 0.08 0.040], 'String', 'hand on ice', ...
        'FontSize', 11, 'HorizontalAlignment', 'left', ...
        'TooltipString', 'Name of the stimulus, then press MARK when it starts');
    hStimBtn = uicontrol('Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.092 0.905 0.045 0.040], 'String', 'MARK', ...
        'FontSize', 11, 'FontWeight', 'bold', 'BackgroundColor', [0.9 0.3 0.65], ...
        'ForegroundColor', 'w', 'Enable', 'off', ...
        'TooltipString', 'Mark the start of the stimulus typed on the left', ...
        'Callback', @(~, ~) markStimulus(hStimEdit));

    % --- BIG LDR READOUT (V4) ---
    % The small on-feed overlay is easy to lose against the eye image. This is
    % the one to glance at when checking the sensor is alive. It reports
    % STALE rather than a number if packets stop, so "reading 0" and
    % "receiving nothing" cannot be confused.
    % Line 2 carries the IR LED state deliberately: if the LEDs are what shifts
    % the LDR reading, the correlation has to be visible in one glance rather
    % than inferred across runs.
    hLdrBig = uicontrol('Style', 'text', 'Units', 'normalized', ...
        'Position', [0.56 0.950 0.15 0.046], ...
        'BackgroundColor', [0.1 0.1 0.1], 'ForegroundColor', [0.6 0.6 0.6], ...
        'FontSize', 12, 'FontWeight', 'bold', 'HorizontalAlignment', 'center', ...
        'String', {'LDR: waiting...', ''});
    uicontrol('Style', 'pushbutton', 'Units', 'normalized', ...
        'Position', [0.712 0.952 0.06 0.042], 'String', 'Calibrate', ...
        'FontSize', 10, 'FontWeight', 'bold', 'BackgroundColor', [0.35 0.35 0.75], ...
        'ForegroundColor', 'w', 'TooltipString', 'Re-measure room vs dark for the LDR', ...
        'Callback', @(~, ~) setCalReq());

    % --- BIG TEMPERATURE READOUT (NEW IN V5) ---
    % Object first: that is the one being measured. Same staleness treatment as
    % the LDR, and for the same reason — a frozen number and a genuinely steady
    % temperature look identical, which for a thermometer is most of the time.
    hTempBig = uicontrol('Style', 'text', 'Units', 'normalized', ...
        'Position', [0.775 0.950 0.215 0.046], ...
        'BackgroundColor', [0.1 0.1 0.1], 'ForegroundColor', [0.6 0.6 0.6], ...
        'FontSize', 12, 'FontWeight', 'bold', 'HorizontalAlignment', 'center', ...
        'String', {'TEMP: waiting...', ''});

    % --- BIG BPM READOUT (NEW IN V6) ---
    % Sits between the IR button and the LDR readout, in the gap that was empty.
    % Second line carries the beat count and the age of the last beat: a plain
    % number cannot distinguish "resting at 62" from "detector stalled at 62".
    hBpmBig = uicontrol('Style', 'text', 'Units', 'normalized', ...
        'Position', [0.385 0.950 0.17 0.046], ...
        'BackgroundColor', [0.1 0.1 0.1], 'ForegroundColor', [0.6 0.6 0.6], ...
        'FontSize', 12, 'FontWeight', 'bold', 'HorizontalAlignment', 'center', ...
        'String', {'BPM: --- (no finger)', ''});

    % --- IR TOGGLE BUTTON ---
    hIRBtn = uicontrol('Style', 'togglebutton', 'Units', 'normalized', ...
        'Position', [0.01 0.952 0.15 0.042], 'FontSize', 12, 'FontWeight', 'bold', ...
        'String', '⬤  IR LEDs: OFF  [NORMAL]', 'BackgroundColor', [0.08 0.75 0.08], ...
        'ForegroundColor', [0 0 0], 'Callback', @toggleIRMode);

    % --- PER-MINUTE TRENDS WINDOW (NEW IN V6) ---
    % Separate window, not more panels on the live figure. These update once a
    % minute against traces that update 8-30 times a second; sharing a figure
    % would mean redrawing five static panels on every frame refresh, which is
    % exactly the graphics cost that starved the serial read earlier in this
    % project. Closing it is harmless — every write to it is guarded.
    figMin = figure('Name', 'Per-Minute Trends', 'NumberTitle', 'off', ...
                    'Color', [0.08 0.08 0.08], 'Position', [180 100 1100 800]);
    PerMinutePanels(figMin, struct(), 'Per-Minute Trends — waiting for minute 1');

    % V7: legends never change after setup; auto-update ran on every plot
    % update (11k callbacks in 70 s of profiling).
    set(findobj(fig, 'Type', 'legend'), 'AutoUpdate', 'off');
    figure(fig);   % keep the live view focused
    drawnow;

    % --- 3. PARAMETERS ---
    global saveTrigger;    saveTrigger   = false;
    global startReq;       startReq      = false;   % START button pressed
    global stopReq;        stopReq       = false;   % STOP pressed AND confirmed
    global calReq;         calReq        = false;   % LDR Calibrate pressed
    global pauseReq;       pauseReq      = false;   % PAUSE toggled on
    global userClosedPx;   userClosedPx  = NaN;     % closed-threshold slider; NaN = not moved
    global stimPending;    stimPending   = cell(0, 2);   % {wall time, name} not yet logged
    global resetBaseline;  resetBaseline = false;
    global eyeCalReq;      eyeCalReq     = false;   % Eye calibrate pressed
    global irMode;         irMode        = false;   % false=NORMAL, true=IR/DARK (manual button / LED state)
    global switchMode;     switchMode    = false;   % triggers threshold swap + re-seed
    % NEW IN V4: which threshold set the swap block should install. Decoupled
    % from irMode because there are now TWO things that can request a swap —
    % the manual button and an LDR transition into DARK — but only the button
    % owns the physical IR LEDs.
    global useDarkThresholds; useDarkThresholds = false;

    camRotation = 0;

    % --- LDR AMBIENT LIGHT (NEW IN V4) ---
    % Thresholds and hysteresis shared with LDR_LightClassifier.m, derived
    % from LDR_calibration_data_esp32.csv (n=141, 10 Aug 2026, THIRD capture):
    %   Dark   n=46  mean 184.8  sd 62.0  range  64-292   median 195
    %   Indoor n=46  mean 758.4  sd 18.0  range 703-783   median 766
    %   Bright n=49  mean 946.1  sd 69.8  range 705-1023  median 949
    % POLARITY: non-inverted, HIGHER = BRIGHTER. Shading the sensor lowers it.
    %
    % THIS IS THE THIRD SET OF NUMBERS FOR THIS SENSOR. The divider's operating
    % point has moved every time it was rebuilt (Uno 5V: indoor 827; ESP32 v1:
    % indoor 93; ESP32 v2: indoor 758). The thresholds below are worthless the
    % moment that circuit is rewired again — recapture and re-derive if it is.
    %
    % DARK|INDOOR at 497: excellent. 411-count empty gap (dark max 292, indoor
    %   min 703), zero errors, ~165 counts of margin on each side. This is the
    %   boundary that matters, because DARK is what drives the IR LEDs.
    % INDOOR|OUTSIDE at 815: the classes overlap by 78 counts, but only via
    %   three dim-outdoor samples (705, 781, 792). Ignoring those, indoor tops
    %   out at 783 and the bright cluster starts at 851, so 815 sits mid-gap
    %   with ~20 counts either side. Those three read INDOOR — which costs
    %   nothing, since INDOOR and OUTSIDE both mean "LEDs off". Only DARK vs
    %   not-DARK changes behaviour; the upper boundary is display only.
    ldrT.darkLo = 457;  ldrT.darkHi = 537;   % 497 -/+ 40
    ldrT.briLo  = 800;  ldrT.briHi  = 830;   % 815 -/+ 15
    lightNames  = {'DARK', 'INDOOR', 'OUTSIDE / BRIGHT'};
    lightCols   = {[0.45 0.45 0.95], [0.95 0.85 0.35], [0.95 0.60 0.20]};
    lightState  = 0;        % 0 = no LDR packet seen yet
    ldrVal      = NaN;      % latest raw reading (0-1023)
    lastLdrTime = -Inf;     % toc() of the last LDR packet — drives the STALE warning
    ldrStaleSec = 2.0;      % no packet for this long => something is wrong (expect 10 Hz)

    % Automatic IR LED switching. See OSCILLATION GUARD in the header — the
    % LDR can see the IR LEDs it just switched on.
    % ponytail: a flat time lockout, not a model of the optical coupling.
    % If it still chatters, move the LDR away from the emitters or shade it
    % from them; widening this only makes the chatter slower, not gone.
    % AUTOMATIC IR LED SWITCHING — DARK turns the IR LEDs on, INDOOR/OUTSIDE
    % turns them off. Re-enabled 2026-08-11 after the intermittent contact in
    % the LDR divider was repaired; it was held off during that fault so the
    % LEDs could not confound the readings.
    %
    % Set false to go back to manual-only (the toggle button) if the LEDs ever
    % need to be taken out of the loop for debugging again.
    AUTO_IR = true;

    irAutoLockout    = 3.0;   % seconds to ignore auto LED changes after one fires
    lastIRAutoSwitch = -Inf;  % toc() timestamp of the last automatic LED change
    pendingDark      = false; % what the light state is asking the LEDs to be
    havePending      = false; % ...and whether that request is still outstanding
    % One row per calibration: [time_s (NaN = before START), room, dark, darkLo, darkHi]
    ldrCalLog        = zeros(0, 5);
    ldrTimeLog  = [];       % logged for the session CSV
    ldrValLog   = [];
    ldrStateLog = [];

    % --- HEART WAVEFORM (NEW IN V5) ---
    % Fixed-length scrolling window, same 2000 samples Heart_Waveform_1.m used
    % (~20 s at 100 Hz). Preallocated and shifted rather than grown, because
    % this is redrawn every frame and a growing array would reallocate ~25x/s.
    % 600 samples = 6 s at the MEASURED 100 Hz output rate.
    %
    % The firmware asks for 400 Hz / SAMPLE_AVERAGE 2, which should give 200 Hz,
    % but the boot census counts 501 samples in 5 s twice running — exactly
    % 100 Hz. The MAX30102 will not sustain 400 Hz in SpO2 mode at 411 us pulse
    % width (two LEDs must each be sampled), so the chip clamps. Sizing the
    % window off the requested rate rather than the measured one would show 12 s
    % of trace while the label claimed 6.
    %
    % Note the back-dating below already divides by 100, which is correct for
    % the real rate — that was right by accident, not design.
    HEART_WIN   = 600;
    BPM_STALE_SEC = 3.0;   % no beat for this long -> show '---', not a stale number
    heartFilt   = zeros(1, HEART_WIN);
    heartDeriv  = zeros(1, HEART_WIN);

    % --- BPM DETECTOR STATE (NEW IN V6) ---
    % Beats are detected on the FIRST DERIVATIVE, not the filtered signal.
    % The systolic upstroke is the sharpest, most repeatable feature in a PPG;
    % the signal peak itself is rounded and the dicrotic notch can split it,
    % which double-counts. Max-upslope is what pulse oximeters actually use.
    %
    % Streaming, one pass over each arriving batch — NOT findpeaks() on the
    % display window. That window is a rolling 600-sample buffer redrawn on its
    % own cadence, so re-scanning it would count the same beat repeatedly and
    % miss any beat that fell between redraws. It also keeps this toolbox-free:
    % findpeaks needs Signal Processing, median does not.
    bpmEnv        = [];     % beat-detector state (ppgBeats)
    bpmArmed      = true;   % hysteresis latch, blocks re-trigger on one peak
    bpmLastBeatT  = -inf;   % time of the last accepted beat
    bpmIbi        = [];     % recent inter-beat intervals, seconds
    bpmValue      = NaN;    % current estimate
    beatTimes     = [];     % accepted beat times, for the saved session
    beatSampIdx   = [];     % global sample index of each beat, for the markers
    bpmTimeLog    = [];     % BPM vs time, one entry per accepted beat
    bpmValLog     = [];
    heartX      = 1:HEART_WIN;
    nHeart      = 0;         % total samples received this session
    lastHeartTime = -Inf;
    heartStaleSec = 2.0;

    %% ===== DIAG BRIDGE (removable) =====
    dg_heartPkts   = 0;      % heart packets decoded
    dg_lastDiag    = -Inf;   % last console dump
    dg_lastBatchN  = 0;      % samples in the most recent packet
    dg_rawMin      = 0;      % raw decoded values BEFORE negation
    dg_rawMax      = 0;
    %% ===== END DIAG BRIDGE =====
    % Full-rate log for the .mat. 100 Hz x 20 min is 120k doubles per channel,
    % about 1 MB each — worth keeping, since the whole point of merging is
    % being able to line the PPG up against the blinks afterwards.
    %
    % PREALLOCATED, unlike every other log in this file. The others append at
    % the frame rate (~25 Hz); this one appends at 100 Hz inside the same loop,
    % and growing a 120k array a few elements at a time is where MATLAB's
    % reallocation cost actually becomes visible as frame jitter. Doubles if a
    % session outruns it, so the cap is a starting size and not a limit.
    heartCap      = round(20 * 60 * 110);   % starting size (20 min at 100 Hz + 10%); doubles when outgrown
    heartTimeLog  = zeros(1, heartCap);
    heartFiltLog  = zeros(1, heartCap);
    heartDerivLog = zeros(1, heartCap);
    nHeartLog     = 0;      % samples actually written; slice with 1:nHeartLog

    % --- TEMPERATURE (NEW IN V5) ---
    tempAmb      = NaN;
    tempObj      = NaN;
    lastTempTime = -Inf;
    tempStaleSec = 3.0;      % firmware sends at 2 Hz, so 3 s is ~6 missed packets
    tempTimeLog  = [];
    tempAmbLog   = [];
    tempObjLog   = [];

    % --- SUBSYSTEM STATUS + CAMERA LIVENESS (NEW IN V5) ---
    % Every channel is independent on the firmware side, so the host must be
    % able to run with any subset present. These track what is actually alive.
    statusMask    = 0;        % latest status bitmask; 0 = none received yet
    lastStatTime  = -Inf;     % only tested against -Inf, for "never seen"
    % No staleness timer on the status packet itself: if the board stops, all
    % four per-channel staleness indicators light up, which says the same thing
    % with more detail. One more timer here would be noise.
    lastFrameTime = -Inf;     % camera liveness, independent of the status byte
    frameStaleSec = 2.0;
    nFrames       = 0;
    lastFrameMean = 0;        % mean pixel value — separates "black" from "dim"
    rxBuf         = zeros(1, 0);   % raw byte accumulator for the bulk reader
    dg_flushes    = 0;             % runaway-guard trips
    dg_rxBytes    = 0;             % total bytes pulled off the port
    dg_rxPrev     = 0;             % for the per-second rate
    dg_reads      = 0;             % serialport read() calls issued

    % The display refresh no longer lives in the camera path (V4 drew the LDR,
    % temperature and heart readouts only after a frame arrived, so no camera
    % meant no sensor displays at all). It now runs on its own cadence.
    % Points kept on screen for the blink/centroid axes. Those axes show a 60 s
    % window; at the firmware's ~15 fps cap that is ~900 points, so 1200 covers
    % it with margin even if the frame rate rises. Only affects DRAWING — the
    % logs written to the .mat and CSVs are still complete.
    PLOT_TAIL = 1200;
    % --- V7 CAMERA CONTROL ---
    FRAME_MS     = 40;          % ms between frames requested from the board (25 fps)
    UI_PERIOD    = 0.1;         % s between live plot redraws (tracking runs every frame)
    AUTO_EXPOSE  = true;        % closed-loop exposure (see header)
    EXPO_TARGET  = 200;         % wanted 98th-percentile raw brightness of the eye image
    EXPO_MAX_GAIN = 8;          % gain is only raised once exposure is maxed; noise grows with it
    aecValue     = 300;         % firmware start values (FIXED_AEC_VALUE / FIXED_AGC_GAIN)
    agcGain      = 2;
    expoHiAvg    = NaN;         % EMA of the raw 98th percentile
    lastExpoAdj  = 0;           % toc of the last exposure check
    exposureLog  = zeros(0, 3); % [t aec gain] at START and at every change
    lastUiDraw   = -Inf;

    lastUiRefresh = -Inf;
    uiRefreshSec  = 1 / 15;   % 15 Hz is plenty for numeric readouts

    % The PPG traces get their own faster tick. Numbers are fine at 15 Hz; a
    % 200 Hz waveform redrawn at 15 fps visibly steps.
    lastHeartDraw = -Inf;
    heartDrawSec  = 1 / 30;

    % Eye presence
    noEyeThresh     = 160;
    noEyeLimit      = 15;
    eyePresentLimit = 5;
    noEyeCount      = 0;
    eyePresentCount = 0;
    eyePresent      = false;

    % Lighting normalisation (see header). The range is smoothed over ~1.5 s
    % so it follows the room's lighting, not a blink.
    NORM_LO = 60; NORM_SPAN = 140;   % where a typical well-lit frame's 2..98% range sits
    NORM_ALPHA   = 0.05;             % per-frame EMA of the range (~1.5 s at 15 fps)
    NORM_MAXGAIN = 4;                % ponytail: caps noise amplification in dim light; raise
                                     % if dim scenes still lose the pupil, lower if the
                                     % motion signal gets noisy in the dark
    MIN_CONTRAST = 15;               % raw 2..98% range below this = too dark to track
    normLo = NaN; normSpan = NaN;
    lastRawSpan = NaN; lastWashed = false;

    % Pupil centroid (now the tracking signal)
    darkPixelThresh = 80;
    centXsmooth = roiSize / 2;
    centYsmooth = roiSize / 2;
    centSmAlpha = 0.8;        % light smoothing: 0.5 + a 5-frame median added ~0.3-0.4 s to every blink's end

    % V7: eye centre (calibrated at START, then the median open-eye position
    % of the last ~3 s -- see EYE TRACKER). Names kept for the logs/plotter.
    centXbaseline       = roiSize / 2;
    centYbaseline       = roiSize / 2;
    baselineReady       = false;
    % Closing direction (unit vector, image x/y; y = down). Learnt from the
    % centroid's largest excursion during each motion-detected blink; see
    % closeDirFrom. Starts straight down = the original behaviour.
    closeDir    = [0 1];
    closeDirBuf = zeros(0, 2);   % unit excursion vectors of recent blinks
    peakVec     = [0 0];         % largest excursion during the current motion peak
    centXlog    = [];            % dark-pixel centroid X (smoothed), one per logged frame
    pupilXlog   = [];            % pupil position used for gaze; NaN = not usable
    pupilYlog   = [];
    gazeStr     = 'Gaze: tracking...';
    gazeBuf     = nan(3, 2);         % last 3 dark-spot positions (gaze = their median)
    suppressLog = [];            % head-move filter active on that frame
    pauseLog    = zeros(0, 2);   % [start end] of each pause, seconds since START
    closedLog   = zeros(0, 2);   % [t px] closed threshold in force from t (slider / mode switch)
    stimTimes   = zeros(1, 0);   % stimulus marks, seconds since START
    stimNames   = cell(1, 0);    % ... and what each one was

    % Duration detection -- applied to AMPLIFIED centroid deviation.
    % SIGNED test: only the DOWN direction (centY increases past baseline)
    % counts as a closure. Looking UP makes the centroid go UP (Y decreases)
    % which used to false-trip the old |.| test; that direction is now ignored.
    closedDeltaPx = 30;   % V7: eye closure % above this -> CLOSED (name kept for the logs/plotter)
    openMarginPx  = 15;   %     below this -> OPEN

    eyeClosedY    = false;
    lidCloseTime  = 0;
    hDurLabels    = gobjects(0);   % on-plot blink-duration labels (last 60 s only)
    % --- V7 EYE TRACKER state (see EYE TRACKER in the loop) ---
    [eyeXX, eyeYY] = meshgrid(1:roiSize);
    EYE_WIN    = 22;              % px: blob centres searched within this of the eye centre
    EYE_CAL_N  = 40;              % frames (~1.6 s) to calibrate where the eye is
    eyeCalXY   = zeros(0, 2);     % calibration positions
    eyePosBuf  = nan(75, 2);      % last ~3 s of open-eye positions -> eye centre
    eyePosIdx  = 0;
    openBuf    = nan(1, 75);      % last ~3 s of blob strengths -> open reference (median)
    openIdx    = 0;
    closureBuf = zeros(1, 3);     % last 3 closure % values (median = re-open test)
    closedBelowSince = NaN;  % while CLOSED: when green last went back under the red line
    REOPEN_HOLD   = 0.3;     % s under the red line that ends a closure (see detector)
    MAX_CLOSED    = 30;      % s closed -> marker assumed stuck, reset automatically
    % --- OPEN/CLOSED TEMPLATES (Eye calibrate button; see EYE TEMPLATES) ---
    tplOpen = []; tplClosed = [];   % mean normalised frame, eyes open / closed
    tplPhase = 0; tplT0 = 0;        % 0 idle, 1 recording open, 2 recording closed
    tplAcc = zeros(roiSize^2, 1); tplN = 0; tplOpenAcc = [];
    tplBuf = zeros(1, 3);           % last 3 template scores (>0 = looks closed)
    tplOpenSince = NaN;             % while CLOSED: when the frames first looked open
    CHECKPOINT_SEC = 300;    % crash-safety save interval (see CHECKPOINT)
    SAVE_FRAMES    = true;   % raw camera frames to frames_<tag>.bin (see START)
    lastCheckpoint = 0;

    % Display-freeze of the centroid crosses during a held closure. The
    % centroid is still computed internally (needed for re-open detection),
    % but the displayed red/green crosses lock at their closure-onset position
    % so the wild eyelash wander doesn't pollute the live view.

    % Head movement detection (suppresses false transitions)
    splitRow                = round(roiSize / 2);
    headMoveUpperFracThresh = 0.50;
    headMoveMinMotion       = 5;
    % MOTION NOISE FLOOR. The 0.18 threshold / 0.108 release were set for a
    % resting motion level of ~0.05. In dim light camera noise lifted the
    % resting level to ~0.12 (16:22 replay): above the release, so a motion
    % blink began and never finished (2 counted vs 32 closures), and noise
    % alone passed headMoveMinMotion (head-move flag on 38% of frames).
    % Both are now measured from the floor; in good light nothing changes.
    motionFloor             = 0.05;   % slow EMA of the resting motion level
    peakStart               = 0;      % when the current motion peak began
    headMoveCooldownFrames  = 8;
    headMoveCooldown        = 0;
    isHeadMove              = false;
    suppressDetection       = false;
    upperFracBuf            = ones(1, 3) * 0.5;

    % Blink (motion-peak) detection -- simple from old version
    peakThreshold = 0.18;
    minBlinkGap   = 0.2;
    smoothWindow  = 3;

    inPeak          = false;
    lastBlinkTime   = 0;
    blinkCount      = 0;
    blinkTimestamps = [];
    blinkIntervals  = [];
    blinkDurations  = [];

    % --- SECOND, INDEPENDENT blink counter (centroid threshold crossing) ---
    % Counts a blink each time the amplified centroid crosses DOWN past the
    % closed threshold (closure onset). The closed/open state itself is the
    % shared eyeClosedY flag (Schmitt-trigger hysteresis: close >closedDeltaPx,
    % re-open <openMarginPx), which ALSO drives the closure-duration timer.
    %
    % centMinGap: minimum time between consecutive centroid closures. Acts
    % as a debounce against the post-blink "wobble" -- during recovery from
    % a closure the instantaneous green centroid can briefly re-cross the
    % closed threshold while it settles, which would otherwise fire a
    % spurious second close right after the real one. Real physiological
    % blinks are never closer than ~250 ms.
    centBlinkCount      = 0;
    centBlinkTimestamps = [];
    centBlinksThisMin   = 0;
    minBinCentCounts    = [];
    centMinGap          = 0.25;
    lastCentBlinkTime   = -Inf;

    % Per-minute stats
    minuteBin         = 1;
    minBinCounts      = [];
    minBinAvgInterval = [];
    minBinAvgDur      = [];
    minBinMaxDur      = [];
    minBinGazeShifts  = [];     % gaze shifts per minute of tracked time
    minBinFixDur      = [];     % median fixation length, s
    minBinGazeMove    = [];     % mean gaze movement, px/s
    blinksThisMin     = 0;
    intervalsThisMin  = [];
    durationsThisMin  = [];
    minBinBpm         = [];     % mean BPM per minute        (NEW IN V6)
    minBinTempObj     = [];     % mean object temp per minute (NEW IN V6)
    minBinTempAmb     = [];     % mean ambient temp per minute

    timeLog     = [];
    diffLog     = [];
    centYlog    = [];   % raw smoothed centroid Y (red)
    centYampLog = [];   % amplified centroid Y (green, used for detection)
    baseYlog    = [];   % rolling baseline (yellow)
    diffBuffer = zeros(1, smoothWindow);
    prevFrame  = zeros(roiSize, roiSize);
    nResync    = 0;                       % was never initialised: first resync crashed the loop

    % --- INITIAL SYNC ---
    flush(s); pause(0.2); flush(s);

    % TIME-BASED, not attempt-based, and bulk reads rather than byte-at-a-time.
    %
    % The old loop gave up after 10000 attempts, and every idle pass through
    % pause(0.001) burned one. At boot the board is SILENT for a while — USB CDC
    % settle, the boot report, and (when enabled) the 8 s PPG census — so the
    % counter expired at roughly the moment streaming began. That is a race, and
    % it lost most of the time: startup failed ~7 runs in 10 and needed repeated
    % board resets.
    %
    % A wall-clock deadline cannot be exhausted by waiting, and reading the whole
    % buffer at once instead of one byte per call makes the search essentially
    % free. Syncing on ANY legal header still matters: a board with no camera
    % streams perfectly good LDR/heart/temperature packets, and the 1 Hz status
    % packet guarantees there is always something to catch.
    SYNC_TIMEOUT_SEC = 30;
    syncFound = false;
    for attempt = 1:2
        % The board was just reset (boots in ~3.5 s). Attempt 1 waits 15 s;
        % if it is still silent it is reset once more and given the full 30 s.
        waitSec = 15; if attempt == 2, waitSec = SYNC_TIMEOUT_SEC; end
        fprintf('Waiting for sync (up to %d s — the board is quiet while it boots)...\n', waitSec);
        syncBuf = zeros(1, 0);
        tSync   = tic;
        while ~syncFound && toc(tSync) < waitSec
            keepAlive(s);
            k = s.NumBytesAvailable;
            if k > 0
                syncBuf = [syncBuf, read(s, k, "uint8")]; %#ok<AGROW>
                for hb = 251:255
                    if ~isempty(strfind(syncBuf, [255 0 hb 0])) %#ok<STREMP>
                        syncFound = true; break;
                    end
                end
                if numel(syncBuf) > 20000, syncBuf = syncBuf(end-3:end); end
            else
                pause(0.02);
            end
        end
        if syncFound || attempt == 2, break; end
        fprintf('No data from the board -- resetting it over USB and reconnecting...\n');
        s = resetBoard(s, port, baud);
        if isempty(s), break; end
        write(s, uint8(0xF0), 'uint8');
    end

    if ~syncFound
        fprintf(['Could not sync — nothing recognisable in %d s.\n' ...
                 '  - Is Simplified_eye_V6_code.ino actually flashed?\n' ...
                 '  - Is the Arduino Serial Monitor CLOSED? It steals the stream.\n' ...
                 '  - Does the boot report show subsystems OK?\n'], SYNC_TIMEOUT_SEC);
        if ~isempty(s), delete(s); end
        return;
    end
    % Alignment does not matter here: the main loop reads a header and resyncs
    % on its own, so start it from a clean buffer.
    flush(s);
    fprintf('Sync found! Starting V5 session (eye + LDR + heart + temperature).\n');
    sendCamera(s, FRAME_MS, aecValue, agcGain);
    fprintf('Camera: %d ms per frame (%.0f fps max), exposure %d, gain %d\n', FRAME_MS, 1000 / FRAME_MS, aecValue, agcGain);

    % Store serial port in figure so toggleIRMode callback can reach it
    set(fig, 'UserData', s);

    % NOTE: V4 read a full 6400-byte frame here, assuming the packet it synced
    % on was always a camera frame. That is no longer true — we may well have
    % landed on a status or LDR header — so the payload is left to the main
    % loop's dispatch, which reads whatever the header actually implies.
    eyeImg = zeros(roiSize, roiSize);
    drawnow;

    % --- WAIT FOR START ---
    % The board is synced and streaming; nothing is recorded until START.
    % Closing the window here ends without saving (there is nothing to save).
    set(hTimer, 'String', 'Press START', 'ForegroundColor', [1 1 0.4]);
    fprintf('Board ready. Press START in the window to begin recording.\n');
    set(hStartBtn, 'Enable', 'on');
    % Light-sensor calibration first (the Calibrate button still works later).
    if strcmp(questdlg(['Calibrate the light sensor before recording?' newline newline ...
            'Takes ~10 s: room light, then you cover the sensor.'], 'Before START', ...
            'Calibrate', 'Skip', 'Calibrate'), 'Calibrate')
        setCalReq();
    end
    while ishandle(fig) && ~startReq
        keepAlive(s);
        if calReq
            calReq = false;
            applyIRMode(hIRBtn, s, false); irMode = false;
            [ldrT, calOk, calRoom, calDark] = calibrateLdr(s, hLdrBig, ldrT);
            if calOk
                ldrCalLog(end+1, :) = [NaN, calRoom, calDark, ldrT.darkLo, ldrT.darkHi]; %#ok<AGROW>
                lightState = 0;                   % reclassify from the next reading
            end
            set(hTimer, 'String', 'Press START');
        end
        pause(0.05);
    end
    if ~ishandle(fig)
        fprintf('Window closed before START -- nothing recorded.\n');
        try, write(s, uint8(0xF0), 'uint8'); catch, end
        delete(s); return;
    end
    set(hStartBtn, 'Enable', 'off');
    for k = 3:-1:1
        set(hTimer, 'String', sprintf('Starting in %d', k), 'ForegroundColor', [1 0.6 0.2]);
        drawnow; pause(1);
        if ~ishandle(fig), delete(s); return; end
    end
    sessionTag = datestr(datetime('now'), 'yyyy-mm-dd_HHMMSS');
    saveFolder = fullfile(getenv('HOME'), 'Desktop', 'MyDataFolder', ['session_' sessionTag]);
    if ~exist(saveFolder, 'dir'), mkdir(saveFolder); end
    fprintf('RECORDING. Session %s -> %s\n', sessionTag, saveFolder);
    % Bytes that piled up while waiting are not part of the recording.
    flush(s); rxBuf = zeros(1, 0);
    startTime = tic;
    sessionStartWall = datetime('now');   % clock time of time_s = 0, for the plotter
    % RAW CAMERA FRAMES to disk (~185 MB per hour): every 80x80 frame as
    % uint8 + its time, so tracking can be re-run and tuned offline on real
    % eyes. Read back: f = fopen(...frames.bin); v = fread(f, [6408 Inf], 'uint8=>uint8');
    % each column = 8-byte double time_s, then 6400 pixels row by row.
    fidFrames = -1;
    if SAVE_FRAMES
        fidFrames = fopen(fullfile(saveFolder, sprintf('frames_%s.bin', sessionTag)), 'w');
    end
    set(hTimer, 'ForegroundColor', [1 0.35 0.35]);
    set([hStopBtn hPauseBtn hStimBtn], 'Enable', 'on');
    exposureLog(1, :) = [0, aecValue, agcGain];
    closedLog(1, :) = [0 closedDeltaPx];

    exitReason = 'interrupted';

    try
        while ishandle(fig)
            keepAlive(s);

            % --- THRESHOLD MODE SWITCH (checked first, every frame) ---
            global irMode switchMode useDarkThresholds;

            % --- LDR CALIBRATION (button) ---
            % Blocking for ~6 s plus however long the dialog is open; the
            % stream is not read meanwhile, so that stretch is a gap in the
            % logs. The LEDs go off first and the NORMAL thresholds come back
            % with them, exactly as a manual IR-off click does.
            if calReq
                calReq = false;
                applyIRMode(hIRBtn, s, false);
                irMode = false; havePending = false;
                if useDarkThresholds, useDarkThresholds = false; switchMode = true; end
                [ldrT, calOk, calRoom, calDark] = calibrateLdr(s, hLdrBig, ldrT);
                if calOk
                    ldrCalLog(end+1, :) = [toc(startTime), calRoom, calDark, ldrT.darkLo, ldrT.darkHi]; %#ok<AGROW>
                    lightState = 0;
                end
                rxBuf = zeros(1, 0);              % calibration consumed the stream
                lastIRAutoSwitch = toc(startTime);
            end

            if switchMode
                if useDarkThresholds
                    % --- IR/DARK THRESHOLD SET ACTIVE ---
                    % Selected by the manual IR button, or automatically when
                    % the LDR classifies the ambient light as DARK.
                    noEyeThresh = 255;
                    % Was 40, tuned on RAW IR frames (very dark pupil). Frames
                    % are now normalised first, so the same 80 applies in both
                    % modes; 40 would now select almost nothing.
                    darkPixelThresh = 80;
                    closedDeltaPx = 30; openMarginPx = 15; peakThreshold = 0.18;   % V7: % closure
                    modeStr = 'IR/DARK';
                else
                    % --- NORMAL (interior light) THRESHOLD SET ACTIVE ---
                    noEyeThresh = 160; darkPixelThresh = 80;
                    closedDeltaPx = 30; openMarginPx = 15; peakThreshold = 0.18;   % V7: % closure
                    modeStr = 'NORMAL';
                end
                % A slider setting outlives mode switches; otherwise show the mode's value.
                if ~isnan(userClosedPx)
                    closedDeltaPx = userClosedPx; openMarginPx = userClosedPx / 2;
                end
                set(hClosedSld, 'Value', closedDeltaPx);
                set(hClosedSldLbl, 'String', sprintf('%g %%', closedDeltaPx));
                closedLog(end+1, :) = [toc(startTime), closedDeltaPx];
                % Re-seed baseline: darkPixelThresh changed, old centroid position is stale
                % V7: recalibrate the eye AND forget the open reference -- in
                % IR the pupil is the dark blob, in visible light the iris.
                baselineReady = false; eyeCalXY = zeros(0, 2); openBuf(:) = NaN;
                eyePresent = false; noEyeCount = 0; eyePresentCount = 0;
                inPeak = false; eyeClosedY = false;
                closureBuf(:) = 0;
                if ~isempty(tplOpen), fprintf('Eye templates cleared (lighting mode changed): press Eye calibrate again\n'); end
                tplOpen = []; tplClosed = []; tplPhase = 0;
                lastCentBlinkTime = -Inf; headMoveCooldown = 0;
                upperFracBuf = ones(1, 3) * 0.5;
                set(hThreshLine, 'Value', peakThreshold);
                switchMode = false;
                fprintf('Switched to %s mode (noEyeThr=%d darkThr=%d closedDx=%.1f openMgn=%.1f peakThr=%.3f)\n', ...
                    modeStr, noEyeThresh, darkPixelThresh, closedDeltaPx, openMarginPx, peakThreshold);
            end

            % --- STIMULUS MARKS ---
            % Timed by the wall clock at the click, so a mark pressed during a
            % PAUSE still lands at the right moment.
            while ~isempty(stimPending)
                tS = seconds(stimPending{1, 1} - sessionStartWall);
                stimTimes(end+1) = tS; stimNames{end+1} = stimPending{1, 2}; %#ok<AGROW>
                stimPending(1, :) = [];
                for axS = [axBlink, axCent, axTemp]
                    xline(axS, tS, '-', stimNames{end}, 'Color', [1 0.4 0.75], 'LineWidth', 1.5, ...
                          'LabelVerticalAlignment', 'middle', 'FontSize', 9, 'FontWeight', 'bold', ...
                          'HandleVisibility', 'off');
                end
                fprintf('STIMULUS %d at %.1f s: %s\n', numel(stimTimes), tS, stimNames{end});
            end

            % --- CLOSED-THRESHOLD SLIDER ---
            if ~isnan(userClosedPx) && userClosedPx ~= closedDeltaPx
                closedDeltaPx = userClosedPx; openMarginPx = userClosedPx / 2;
                closedLog(end+1, :) = [toc(startTime), closedDeltaPx];
                set(hClosedSldLbl, 'String', sprintf('%g %%', closedDeltaPx));
                fprintf('Closed threshold -> %g px (re-open below %g px)\n', closedDeltaPx, openMarginPx);
            end

            elapsedTotal = toc(startTime);
            if stopReq
                exitReason = 'stopped'; break;
            end

            % --- PAUSE: nothing is recorded; the stream is read and dropped ---
            if pauseReq
                tP = toc(startTime);
                fprintf('PAUSED at %s\n', char(datetime('now', 'Format', 'HH:mm:ss')));
                set(hTimer, 'ForegroundColor', [1 0.8 0.2]);
                while ishandle(fig) && pauseReq && ~stopReq
                    keepAlive(s);
                    if s.NumBytesAvailable > 0, read(s, s.NumBytesAvailable, "uint8"); end
                    dP = toc(startTime) - tP;
                    set(hTimer, 'String', sprintf('PAUSED %02d:%02d', floor(dP / 60), floor(mod(dP, 60))));
                    drawnow; pause(0.1);
                end
                pauseLog(end+1, :) = [tP, toc(startTime)];
                fprintf('RESUMED: pause %d lasted %.0f s\n', size(pauseLog, 1), diff(pauseLog(end, :)));
                flush(s); rxBuf = zeros(1, 0);
                switchMode = true;       % re-seed baseline and detectors, as after a mode swap
                if ishandle(hTimer), set(hTimer, 'ForegroundColor', [1 0.35 0.35]); end
                continue;
            end

            % --- CHECKPOINT every 5 min ---
            % Nothing else reaches the disk before STOP, so a freeze or a
            % force-quit hours in would lose the whole run. One file,
            % overwritten each time; the final session_*.mat is still the
            % file the plotter reads.
            if elapsedTotal - lastCheckpoint >= CHECKPOINT_SEC
                lastCheckpoint = elapsedTotal; tCk = tic;
                try
                    save(fullfile(saveFolder, sprintf('checkpoint_%s.mat', sessionTag)), '-regexp', ...
                        '^(.*Log|.*Timestamps|.*Durations|.*Intervals|minBin.*|.*Count|beatTimes|stim.*|nHeartLog|closeDir|sessionStartWall|sessionTag)$');
                    fprintf('Checkpoint saved at %s (%.1f s)\n', char(datetime('now', 'Format', 'HH:mm:ss')), toc(tCk));
                catch ckErr
                    fprintf('Checkpoint FAILED: %s\n', ckErr.message);
                end
            end

            % --- PER-MINUTE BIN ---
            currentMinute = floor(elapsedTotal / 60) + 1;
            if currentMinute > minuteBin
                minBinCounts(end+1)     = blinksThisMin;
                minBinCentCounts(end+1) = centBlinksThisMin;
                if ~isempty(intervalsThisMin)
                    minBinAvgInterval(end+1) = mean(intervalsThisMin);
                else
                    minBinAvgInterval(end+1) = NaN;
                end
                if ~isempty(durationsThisMin)
                    minBinAvgDur(end+1) = mean(durationsThisMin);
                    minBinMaxDur(end+1) = max(durationsThisMin);
                else
                    minBinAvgDur(end+1) = NaN;
                    minBinMaxDur(end+1) = NaN;
                end

                % BPM and temperature per minute are SLICED FROM THE FULL LOGS
                % by time rather than accumulated in their own counters. Those
                % logs already exist and are already what gets saved, so there
                % is no second running total to fall out of step with them —
                % and a minute with no finger on the sensor lands as NaN by
                % itself instead of needing a special case.
                mT0 = (minuteBin - 1) * 60;
                mT1 = minuteBin * 60;
                minBinBpm(end+1)     = meanInWindow(bpmTimeLog,  bpmValLog,  mT0, mT1);
                minBinTempObj(end+1) = meanInWindow(tempTimeLog, tempObjLog, mT0, mT1);
                minBinTempAmb(end+1) = meanInWindow(tempTimeLog, tempAmbLog, mT0, mT1);
                kG = timeLog >= mT0 & timeLog < mT1;
                G = GazeMetrics(timeLog(kG), pupilXlog(kG), pupilYlog(kG));
                minBinGazeShifts(end+1) = G.rate; minBinFixDur(end+1) = G.fix; minBinGazeMove(end+1) = G.moveMean;

                fprintf(['Min %d: motion %d / centroid %d blinks | avgDur %.3fs | ' ...
                         'maxDur %.3fs | gaze %.1f shifts/min, fixation %.2fs | BPM %.0f | obj %.2fC\n'], ...
                    minuteBin, blinksThisMin, centBlinksThisMin, minBinAvgDur(end), ...
                    minBinMaxDur(end), G.rate, G.fix, minBinBpm(end), minBinTempObj(end));

                % Refresh the trends window. Once a minute, so a full redraw is
                % cheaper than maintaining update paths for five panels.
                if ishandle(figMin)
                    PerMinutePanels(figMin, buildMinuteStruct(minBinCounts, ...
                        minBinCentCounts, minBinAvgDur, minBinMaxDur, minBinGazeShifts, ...
                        minBinFixDur, minBinBpm, minBinTempObj, minBinTempAmb), ...
                        sprintf('Per-Minute Trends — %s (live)', sessionTag));
                end
                % A pause (or a stall) can skip whole minutes: keep one bin
                % per minute so bin k is always minute k.
                for padMin = minuteBin + 1 : currentMinute - 1
                    minBinCounts(end+1) = NaN; minBinCentCounts(end+1) = NaN; %#ok<AGROW>
                    minBinAvgInterval(end+1) = NaN; minBinAvgDur(end+1) = NaN; %#ok<AGROW>
                    minBinMaxDur(end+1) = NaN; minBinBpm(end+1) = NaN; %#ok<AGROW>
                    minBinGazeShifts(end+1) = NaN; minBinFixDur(end+1) = NaN; minBinGazeMove(end+1) = NaN; %#ok<AGROW>
                    minBinTempObj(end+1) = NaN; minBinTempAmb(end+1) = NaN; %#ok<AGROW>
                end
                minuteBin         = currentMinute;
                blinksThisMin     = 0;
                centBlinksThisMin = 0;
                intervalsThisMin  = [];
                durationsThisMin  = [];
            end

            % =============================================================
            % SENSOR DISPLAY REFRESH — runs EVERY iteration, camera or not.
            %
            % V4 drew these three readouts inside the frame path, so a board
            % with no camera showed frozen LDR / temperature / heart displays
            % even while those packets were arriving normally. That coupling
            % was the main host-side independence bug. Everything here depends
            % only on its own channel's data and timestamps.
            %
            % Rate-limited to uiRefreshSec: the numbers are human-readable
            % values, not a waveform, and redrawing them at packet rate cost
            % more than the whole tracking path.
            % =============================================================
            if elapsedTotal - lastUiRefresh >= uiRefreshSec
                lastUiRefresh = elapsedTotal;

                % --- LDR / LIGHT STATE ---
                ldrAge = elapsedTotal - lastLdrTime;
                if lightState == 0
                    set(hLightText, 'String', 'LIGHT: --- (no LDR packets yet)', ...
                        'Color', [0.6 0.6 0.6]);
                    set(hLdrBig, 'String', {'LDR: no packets', ''}, ...
                        'ForegroundColor', [1 0.35 0.35]);
                elseif ldrAge > ldrStaleSec
                    % Packets stopped. Distinguishing this from a genuine low
                    % reading is the whole point of the staleness check — a dead
                    % sensor and a dark room both look like a small number.
                    set(hLightText, 'String', sprintf('LIGHT: %s  (LDR STALE %.0fs)', ...
                        lightNames{lightState}, ldrAge), 'Color', [1 0.35 0.35]);
                    set(hLdrBig, 'String', {sprintf('LDR: STALE %.0fs', ldrAge), ''}, ...
                        'ForegroundColor', [1 0.35 0.35]);
                else
                    set(hLightText, 'String', sprintf('LIGHT: %s  (LDR %d)', ...
                        lightNames{lightState}, round(ldrVal)), ...
                        'Color', lightCols{lightState});
                    set(hLdrBig, 'String', { ...
                        sprintf('LDR: %d      IR: %s', round(ldrVal), ledStr(irMode)), ...
                        lightNames{lightState}}, ...
                        'ForegroundColor', lightCols{lightState});
                end

                % --- TEMPERATURE ---
                % Same three-way treatment as the LDR: never seen / stale / live.
                tempAge = elapsedTotal - lastTempTime;
                if isnan(tempObj)
                    set(hTempBig, 'String', {'TEMP: no packets', ''}, ...
                        'ForegroundColor', [1 0.35 0.35]);
                elseif tempAge > tempStaleSec
                    set(hTempBig, 'String', {sprintf('TEMP: STALE %.0fs', tempAge), ...
                        sprintf('last obj %.2f \xB0C', tempObj)}, ...
                        'ForegroundColor', [1 0.35 0.35]);
                else
                    set(hTempBig, 'String', { ...
                        sprintf('OBJ: %.2f \xB0C', tempObj), ...
                        sprintf('AMB: %.2f \xB0C', tempAmb)}, ...
                        'ForegroundColor', [1.00 0.55 0.15]);
                end

                % --- TEMPERATURE TRACE ---
                % Plotted straight from the session logs, so the live trend and
                % the saved trend are the same data — no second buffer to drift
                % out of step with what actually gets written to the .mat.
                if ~isempty(tempTimeLog)
                    set(hTempObjLine, 'XData', tempTimeLog, 'YData', tempObjLog);
                    set(hTempAmbLine, 'XData', tempTimeLog, 'YData', tempAmbLog);
                    xlim(axTemp, [0, max(elapsedTotal, 10)]);
                    % Padded around BOTH traces, with a 1 degree minimum span.
                    % Skin sits several degrees above ambient and barely moves,
                    % so a tight autoscale on a steady reading magnifies sensor
                    % noise into what looks like a trend.
                    lo = min([tempObjLog, tempAmbLog]);
                    hi = max([tempObjLog, tempAmbLog]);
                    if hi - lo < 1.0
                        mid = (hi + lo) / 2; lo = mid - 0.5; hi = mid + 0.5;
                    end
                    pad = 0.1 * (hi - lo);
                    ylim(axTemp, [lo - pad, hi + pad]);
                end

                % --- HEART WAVEFORM ---
                % Only the TITLE lives on the shared 15 Hz cadence. The traces
                % themselves redraw on their own faster tick further down, or
                % 200 Hz of PPG animates at 15 fps and looks stepped.
                heartAge = elapsedTotal - lastHeartTime;
                if nHeart == 0
                    title(axHeartSig, 'Heart Waveform (MAX30102) — NO PACKETS (sensor absent)', ...
                          'Color', [1 0.35 0.35], 'FontSize', 10, 'FontWeight', 'bold');
                elseif heartAge > heartStaleSec
                    title(axHeartSig, sprintf('Heart Waveform — STALE %.0fs', heartAge), ...
                          'Color', [1 0.35 0.35], 'FontSize', 10, 'FontWeight', 'bold');
                elseif all(heartFilt == 0)
                    % Exactly zero across the whole window. The firmware no
                    % longer gates on a finger threshold — it publishes every
                    % sample — so this is a genuinely flat SIGNAL (nothing on
                    % the sensor, or the settle blanking right after a channel
                    % switch), not a closed gate suppressing real data.
                    title(axHeartSig, 'Heart Waveform — flat (nothing on the sensor?)', ...
                          'Color', [0.95 0.85 0.35], 'FontSize', 10, 'FontWeight', 'bold');
                else
                    title(axHeartSig, sprintf('Heart Waveform (MAX30102) — %d samples', nHeart), ...
                          'Color', 'w', 'FontSize', 10, 'FontWeight', 'bold');
                end

                    %% ===== DIAG BRIDGE (removable) =====
                % One line per second to the Command Window. Prints what MATLAB
                % actually HOLDS, so "the board is fine but nothing plots" can
                % be split into: packets not arriving / arriving as zeros /
                % arriving with data that the axes are not showing.
                if elapsedTotal - dg_lastDiag >= 1.0
                    dg_lastDiag = elapsedTotal;
                    % THROUGHPUT is the number that decides this. The board
                    % sends ~15 frames/s x 6404 B = ~96 kB/s plus the sensor
                    % channels. If rx_kBps is well below that, MATLAB simply
                    % cannot drink from this hose and is permanently discarding
                    % — which loses 95% of everything. That is invisible on the
                    % LDR and temperature (slow scalars) and fatal to a waveform.
                    dgRate    = (dg_rxBytes - dg_rxPrev) / max(elapsedTotal - dg_lastDiag, 1e-3);
                    dg_rxPrev = dg_rxBytes;
                    fprintf(['[HEART] t=%5.1f pkts=%5d samp=%6d lastN=%3d ' ...
                             'lastPkt[%6d %6d] nz=%4d/%d ylim=%.0f | ' ...
                             'rx=%6.1f kB/s backlog=%7d reads/s=%4.0f flush=%d\n'], ...
                        elapsedTotal, dg_heartPkts, nHeart, dg_lastBatchN, ...
                        dg_rawMin, dg_rawMax, ...
                        sum(heartFilt ~= 0), numel(heartFilt), ...
                        max(ylim(axHeartSig)), ...
                        dgRate/1024, numel(rxBuf) + s.NumBytesAvailable, ...
                        dg_reads / max(elapsedTotal, 1e-3), dg_flushes);
                end
                %% ===== END DIAG BRIDGE =====

            % --- CAMERA LIVENESS ---
                % Driven by frame arrivals, not by the status byte, so a camera
                % that initialised but then stopped delivering is still caught.
                frameAge = elapsedTotal - lastFrameTime;
                if nFrames == 0
                    set(hCamText, 'String', 'NO CAMERA — other sensors still live', ...
                        'Color', [1 0.35 0.35]);
                elseif frameAge > frameStaleSec
                    set(hCamText, 'String', sprintf('CAMERA STALE %.0fs', frameAge), ...
                        'Color', [1 0.35 0.35]);
                elseif lastWashed
                    set(hCamText, 'String', 'IMAGE WASHED OUT — too bright for the camera', ...
                        'Color', [1 0.55 0.15]);
                elseif lastRawSpan < MIN_CONTRAST
                    set(hCamText, 'String', sprintf('TOO DARK (contrast %.0f) — IR LEDs / calibrate LDR', ...
                        lastRawSpan), 'Color', [1 0.55 0.15]);
                elseif lastFrameMean < 8
                    % Frames ARE arriving, they are just black. Distinguishing
                    % that from "no camera" matters: one is a dead link, the
                    % other is exposure or illumination. 8/255 is well under any
                    % usable image but above sensor noise.
                    set(hCamText, 'String', sprintf('IMAGE BLACK (mean %.1f) — IR LEDs off?', ...
                        lastFrameMean), 'Color', [1 0.55 0.15]);
                else
                    set(hCamText, 'String', '');
                end

                % The session clock lived in the frame path too, so without a
                % camera the timer froze and the auto-save deadline never fired.
                updateTimer(hTimer, elapsedTotal);
                drawnow limitrate;
            end

            % --- HEART TRACES: own cadence, own robust autoscaling ---
            % robustScale/fitAxis are lifted verbatim from Heart_Waveform_1.m,
            % where they were already tuned. Fixed +/-1500 and +/-10000 limits
            % were why the derivative read as a flat line: a finger arriving
            % steps it by ~2e5, so a max-based axis collapses everything real.
            if elapsedTotal - lastHeartDraw >= heartDrawSec
                lastHeartDraw = elapsedTotal;
                set(hHeartFilt,  'XData', heartX, 'YData', heartFilt);
                % Drawn 5-sample smoothed: the same signal the beat detector uses.
                heartDerivSm = movmean(heartDeriv, [4 0]);
                set(hHeartDeriv, 'XData', heartX, 'YData', heartDerivSm);
                if nHeart > 0
                    % Only WRITE the limit when it actually moves. ylim() forces
                    % an axis recomputation, and calling it unconditionally on
                    % two axes every tick was heavy enough to starve the serial
                    % read: the input buffer then hit the 32000-byte guard, and
                    % flush() threw away the small LDR / temperature / status
                    % packets while the big camera frames still resynced. That
                    % looked exactly like "the camera works but no sensors do".
                    % fitAxis already has hysteresis, so this is usually a no-op.
                    L1 = fitAxis(ylim(axHeartSig), robustScale(heartFilt));
                    if ~isequal(L1, ylim(axHeartSig)), ylim(axHeartSig, L1); end
                    L2 = fitAxis(ylim(axHeartDeriv), robustScale(heartDerivSm));
                    if ~isequal(L2, ylim(axHeartDeriv)), ylim(axHeartDeriv, L2); end
                end
                % --- BPM READOUT AND BEAT MARKERS (NEW IN V6) ---
                % A beat older than BPM_STALE_SEC means the finger left or the
                % lock broke. Showing the last good number forever is the same
                % failure as the frozen temperature readout, so it is dropped.
                beatAge = elapsedTotal - bpmLastBeatT;
                if isnan(bpmValue) || beatAge > BPM_STALE_SEC
                    set(hBpmBig, 'String', {'BPM: --- (no clear pulse)', 'check sensor contact'}, ...
                        'ForegroundColor', [0.6 0.6 0.6]);
                    set(hBpmTxt, 'String', '');
                else
                    set(hBpmBig, 'String', ...
                        {sprintf('BPM: %.0f', bpmValue), ...
                         sprintf('%d beats  |  last %.1fs ago', ...
                                 numel(beatTimes), beatAge)}, ...
                        'ForegroundColor', [1 1 0.3]);
                    set(hBpmTxt, 'String', sprintf('%.0f BPM', bpmValue));
                end

                % Beat markers, mapped from global sample index into the rolling
                % window. Beats older than the window simply fall out of range.
                if ~isempty(beatSampIdx)
                    pos = beatSampIdx - (nHeart - HEART_WIN);
                    keepM = pos >= 1 & pos <= HEART_WIN;
                    set(hBeatMarks, 'XData', heartX(pos(keepM)), ...
                                    'YData', heartDerivSm(pos(keepM)));
                else
                    set(hBeatMarks, 'XData', nan, 'YData', nan);
                end

                % No drawnow here — the shared refresh block and the frame path
                % already issue one per iteration. A third just adds latency to
                % the read loop.
            end

            % RUNAWAY GUARD, not a routine backlog handler.
            %
            % This was numPixels*5 = 32000 bytes, about 0.2 s of stream. At
            % ~160 kB/s it tripped several times a SECOND, and flush() discards
            % the buffer indiscriminately — every heart packet queued in it
            % included. Camera frames survived because they are large and the
            % resync re-finds them; the small packets were annihilated. The
            % measured result was 108 heart samples in 84 s, alternating with
            % STALE, from a board sending ~16800.
            %
            % 20 frames of slack lets a normal backlog drain instead of being
            % nuked. Reaching THIS means something is genuinely wrong.
            % =============================================================
            % BULK READ — ONE serialport transaction per pass.
            %
            % THE reason the heart channel never worked. MATLAB's serialport
            % goes through a USB serial driver with a ~16 ms latency timer per
            % transaction, capping it near 60 read() calls/second no matter how
            % fast the code is. The old parser spent 2-3 read() calls PER PACKET
            % (header, then payload, sometimes a count byte); at ~48 packets/s
            % that is ~120 transactions/s, roughly double the ceiling.
            %
            % Frames survived because one read() fetches all 6400 bytes in a
            % single transaction. The small heart packets lost, because each
            % needed its own transactions and there were not enough to go round.
            % Measured: 1 heart packet received every ~2 s out of ~20/s sent.
            %
            % Reading everything waiting in ONE call and slicing packets out of
            % the resulting array costs ~30 transactions/s total and does not
            % grow as packet types are added.
            % =============================================================
            nAvail = s.NumBytesAvailable;
            if nAvail > 0
                rxBuf      = [rxBuf, read(s, nAvail, "uint8")]; %#ok<AGROW>
                dg_rxBytes = dg_rxBytes + nAvail;
                dg_reads   = dg_reads + 1;
            end

            % Runaway guard. Whole packets only — never a mid-packet cut, which
            % is what made the old flush() corrupt alignment.
            % V7: 60 frames (~2.4 s at 25 fps), was 20 (0.8 s at 25 fps): the
            % once-a-minute trends redraw alone stalled ~1 s and tripped it.
            if numel(rxBuf) > numPixels * 60
                keep  = findHeaderFrom(rxBuf, numel(rxBuf) - numPixels * 4);
                if isempty(keep), rxBuf = zeros(1, 0); else, rxBuf = rxBuf(keep:end); end
                dg_flushes = dg_flushes + 1;
            end
            if numel(rxBuf) < 5
                % limitrate: a full drawnow here re-rendered the whole window
                % on every idle pass (52 of 78 s in the V7 profile).
                drawnow limitrate; pause(0.001); continue;
            end

            % =============================================================
            % PACKET DRAIN — handle MANY packets per pass, not one.
            %
            % This loop used to process exactly ONE packet per outer
            % iteration, and the outer iteration also carries the UI refresh
            % and (for a frame) the whole tracking pipeline. The board sends
            % ~15 frames + ~20 heart + 10 LDR + 2 temp + 1 status = ~48
            % packets/s, so one-per-iteration could not keep pace. The backlog
            % grew until the guard flushed it, and the flush discarded whole
            % buffers — frames survived because they are 6400 bytes and the
            % resync lands inside one almost immediately, while the tiny heart
            % packets were annihilated.
            %
            % Measured before this change: MATLAB received ONE heart packet
            % every ~2 s out of ~20/s sent, about 2.5%, and the survivors were
            % clipped at +/-32767 because the firmware's DC blocker was seeing a
            % gap-ridden signal and reading each gap as a step.
            %
            % Draining in bulk costs nothing: small packets are a few bytes and
            % a couple of reads each. The loop stops at a camera frame so the
            % expensive tracking path still runs once per outer iteration, and
            % at 64 packets so a burst cannot stall the UI indefinitely.
            % =============================================================
            gotFrame = false;
            for dq = 1:64
                if numel(rxBuf) < 5, break; end

            % --- FRAMING: align, then size the packet from its own header ---
            if ~isLegalHeader(rxBuf(1:4))
                idx = findHeaderFrom(rxBuf, 1);
                if isempty(idx)
                    rxBuf = rxBuf(max(1, end-3):end);   % keep a partial header
                    break;
                end
                rxBuf   = rxBuf(idx:end);
                nResync = nResync + 1;
                continue;
            end
            header = rxBuf(1:4);
            need   = packetLength(header(3), rxBuf);
            if need < 0 || numel(rxBuf) < need, break; end   % wait for the rest
            payload = rxBuf(5:need);
            if header(3) ~= 255, rxBuf = rxBuf(need+1:end); end

            % =============================================================
            % LDR PACKET (header byte 3 == 254) -- classify and loop again.
            % Carries no image, so nothing below this point should run.
            % =============================================================
            if header(3) == 254
                b2     = payload;
                ldrVal = double(b2(1)) + 256 * double(b2(2));   % little-endian uint16

                prevLight = lightState;
                if lightState == 0
                    % First reading: classify straight off the boundary
                    % midpoints, there is no previous state to be sticky about.
                    % (Calibrated or default thresholds, whichever is active.)
                    lightState = 1 + (ldrVal >= (ldrT.darkLo + ldrT.darkHi) / 2) ...
                                   + (ldrVal >= (ldrT.briLo  + ldrT.briHi)  / 2);
                else
                    lightState = nextLightState(ldrVal, lightState, ldrT);
                end

                lastLdrTime        = toc(startTime);
                ldrTimeLog(end+1)  = lastLdrTime;
                ldrValLog(end+1)   = ldrVal;
                ldrStateLog(end+1) = lightState;

                % A light-state CHANGE queues an automatic request. Only a
                % change queues one — a manual button press never does, which
                % is what lets a manual override survive until the ambient
                % light next crosses a boundary.
                if lightState ~= prevLight
                    fprintf('Light state -> %s (LDR=%d, IR LEDs %s)\n', ...
                            lightNames{lightState}, round(ldrVal), ledStr(irMode));
                    if AUTO_IR
                        pendingDark = (lightState == 1);   % DARK -> LEDs on + IR thresholds
                        havePending = true;
                    end
                end

                % Apply the queued request, retrying on later packets if the
                % lockout is still running. Without the retry a change that
                % landed inside the lockout would be dropped entirely and the
                % LEDs would stay wrong until the next boundary crossing.
                if havePending
                    [useDarkThresholds, havePending, fireLED] = irAutoResolve( ...
                        havePending, pendingDark, useDarkThresholds, ...
                        toc(startTime) - lastIRAutoSwitch, irAutoLockout);
                    if fireLED
                        switchMode       = true;   % re-seeds baseline, same path as the button
                        irMode           = useDarkThresholds;
                        lastIRAutoSwitch = toc(startTime);
                        applyIRMode(hIRBtn, s, useDarkThresholds);
                        ledNames = {'OFF', 'ON'};
                        setNames = {'NORMAL', 'IR/DARK'};
                        fprintf('  -> auto: IR LEDs %s, %s thresholds\n', ...
                                ledNames{useDarkThresholds + 1}, setNames{useDarkThresholds + 1});
                    end
                end
                continue;
            end

            % =============================================================
            % TEMPERATURE PACKET (header byte 3 == 253) — NEW IN V5.
            % Two int16 LE in hundredths of a degree C. No image, so nothing
            % below this point should run.
            % =============================================================
            if header(3) == 253
                b4      = payload;
                tempAmb = decodeInt16LE(b4(1:2)) / 100;
                tempObj = decodeInt16LE(b4(3:4)) / 100;

                lastTempTime       = toc(startTime);
                tempTimeLog(end+1) = lastTempTime;
                tempAmbLog(end+1)  = tempAmb;
                tempObjLog(end+1)  = tempObj;
                continue;
            end

            % =============================================================
            % HEART BATCH PACKET (header byte 3 == 252) — NEW IN V5.
            % 1 count byte, then count x (int16 filtered, int16 derivative).
            % =============================================================
            if header(3) == 252
                nSamp = double(payload(1));
                if nSamp == 0, continue; end
                raw   = payload(2:end);

                [hf, hd] = decodeHeartBatch(raw);

                %% ===== DIAG BRIDGE (removable) =====
                dg_heartPkts  = dg_heartPkts + 1;
                dg_lastBatchN = nSamp;
                dg_rawMin     = min(hf);
                dg_rawMax     = max(hf);
                %% ===== END DIAG BRIDGE =====
                % NEGATED, matching Heart_Waveform_1.m and the original Python:
                % the PPG's IR reading drops as blood volume rises, so the raw
                % AC signal is upside down. See the header note.
                hf = -hf;
                hd = -hd;

                heartFilt  = [heartFilt(nSamp+1:end),  hf];
                heartDeriv = [heartDeriv(nSamp+1:end), hd];
                nHeart     = nHeart + nSamp;

                lastHeartTime = toc(startTime);

                if nHeartLog + nSamp > numel(heartTimeLog)
                    pad           = zeros(1, numel(heartTimeLog));
                    heartTimeLog  = [heartTimeLog,  pad];
                    heartFiltLog  = [heartFiltLog,  pad];
                    heartDerivLog = [heartDerivLog, pad];
                end
                % One arrival time per batch, back-dated across the samples it
                % holds. The board does not stamp individual samples and the
                % PPG rate is fixed, so evenly spacing them at 100 Hz is exact
                % enough to line the waveform up against blink times.
                idx = nHeartLog + (1:nSamp);
                heartTimeLog(idx)  = lastHeartTime - (nSamp-1:-1:0) / 100;
                heartFiltLog(idx)  = hf;
                heartDerivLog(idx) = hd;
                nHeartLog = nHeartLog + nSamp;

                % --- BEAT DETECTION (NEW IN V6) ---
                % Runs on the batch just decoded, using the same back-dated
                % timestamps written to the log, so beat times line up exactly
                % with the saved waveform and with blink times.
                sampT   = lastHeartTime - (nSamp-1:-1:0) / 100;
                sampIdx = nHeart - nSamp + (1:nSamp);
                [bpmEnv, bpmArmed, bpmLastBeatT, bpmIbi, newBeatT, newBeatIdx] = ...
                    ppgBeats(bpmEnv, bpmArmed, bpmLastBeatT, bpmIbi, hd, sampT, sampIdx);
                if ~isempty(newBeatT)
                    beatTimes   = [beatTimes,   newBeatT];
                    beatSampIdx = [beatSampIdx, newBeatIdx];
                    if steadyPulse(bpmIbi)
                        % MEDIAN, not mean: one missed beat doubles an interval
                        % and one false beat halves it. Either drags a mean far
                        % enough to look like a real change in heart rate; the
                        % median of 8 shrugs both off.
                        bpmValue   = 60 / median(bpmIbi);
                        bpmTimeLog = [bpmTimeLog, newBeatT(end)];
                        bpmValLog  = [bpmValLog,  bpmValue];
                    else
                        bpmValue = NaN;   % beats, but irregular: no number shown
                    end
                end
                continue;
            end

            % =============================================================
            % STATUS PACKET (header byte 3 == 251) — NEW IN V5.
            % One byte of subsystem flags. Carries no measurement, but it is
            % what lets the host tell "sensor absent" from "sensor silent", and
            % it guarantees the link always has traffic to sync on.
            % =============================================================
            if header(3) == 251
                statusMask   = double(payload(1));
                lastStatTime = toc(startTime);
                continue;
            end

                % A camera frame: stop draining and let the outer iteration run
                % the tracking pipeline on it.
                gotFrame = true;
                break;
            end

            % Nothing but small packets this pass — go straight back to the top
            % rather than falling into the frame path with a stale header.
            if ~gotFrame, continue; end

            % --- FRAME: already whole in rxBuf, just take it ---
            rawData = rxBuf(5:4+numPixels);
            if fidFrames > 0
                fwrite(fidFrames, typecast(toc(startTime), 'uint8'), 'uint8');
                fwrite(fidFrames, uint8(rawData), 'uint8');
            end
            rxBuf   = rxBuf(5+numPixels:end);
            nFrames = nFrames + 1;
            lastFrameTime = toc(startTime);
            rawImg  = double(reshape(rawData, [roiSize, roiSize])');
            if camRotation ~= 0, rawImg = rot90(rawImg, round(camRotation / 90)); end

            % --- LIGHTING NORMALISATION (before ANY tracking) ---
            sv = sort(rawImg(:));
            pLo = sv(round(0.02 * numPixels)); pHi = sv(round(0.98 * numPixels));
            lastRawSpan = pHi - pLo;
            if isnan(expoHiAvg), expoHiAvg = pHi; else, expoHiAvg = 0.9 * expoHiAvg + 0.1 * pHi; end
            if AUTO_EXPOSE && toc(startTime) - lastExpoAdj >= 1
                lastExpoAdj = toc(startTime);
                [aecN, gainN] = nextExposure(aecValue, agcGain, expoHiAvg / EXPO_TARGET, EXPO_MAX_GAIN);
                if aecN ~= aecValue || gainN ~= agcGain
                    aecValue = aecN; agcGain = gainN;
                    sendCamera(s, [], aecValue, agcGain);
                    exposureLog(end+1, :) = [toc(startTime), aecValue, agcGain]; %#ok<AGROW>
                    headMoveCooldown = max(headMoveCooldown, 2);   % a brightness step is not a blink
                end
            end
            lastWashed  = mean(rawImg(:) >= 250) >= 0.5;
            if isnan(normLo)
                normLo = pLo; normSpan = max(pHi - pLo, 1);
            else
                normLo   = (1 - NORM_ALPHA) * normLo   + NORM_ALPHA * pLo;
                normSpan = (1 - NORM_ALPHA) * normSpan + NORM_ALPHA * max(pHi - pLo, 1);
            end
            gain   = min(NORM_SPAN / normSpan, NORM_MAXGAIN);
            eyeImg = min(max(NORM_LO + (rawImg - normLo) * gain, 0), 255);
            % 3x3 blur: the gain above amplifies sensor noise too. Unblurred,
            % dim frames (x0.4) lifted the motion signal's noise floor to ~0.11,
            % above the 0.108 release level, so motion blinks never completed
            % and the head-move filter fired constantly. Blinks and the pupil
            % are far larger than 3 px, so tracking is unchanged.
            eyeImg = conv2(eyeImg, ones(3) / 9, 'same');
            eyeImg([1 end], :) = eyeImg([2 end-1], :);   % undo zero-padding darkening at the
            eyeImg(:, [1 end]) = eyeImg(:, [2 end-1]);   % border (it would read as "pupil")

            now = toc(startTime);
            set(hImg, 'CData', eyeImg);      % shows what the tracker sees

            if ~snapshotTaken && now >= snapshotTime
                % uint8: imwrite reads a double image as 0..1, so the old
                % double 0..255 frame was written as a solid white PNG.
                imwrite(uint8(eyeImg), fullfile(saveFolder, sprintf('snapshot_%s.png', sessionTag)));
                fprintf('Snapshot saved\n');
                snapshotTaken = true;
            end

            % =============================================================
            % EYE PRESENCE
            % =============================================================
            frameMean = mean(rawImg(:));
            lastFrameMean = frameMean;   % RAW brightness, surfaced by the camera status readout

            % Was "frameMean > noEyeThresh": a fixed brightness cutoff that
            % normal worn frames (mean 150-177) already sat on. Now: no eye
            % only when the camera has no usable picture at all.
            if lastWashed || lastRawSpan < MIN_CONTRAST
                noEyeCount      = noEyeCount + 1;
                eyePresentCount = 0;
                if noEyeCount >= noEyeLimit
                    eyePresent         = false;
                    noEyeCount         = noEyeLimit;
                    prevFrame          = eyeImg;
                    inPeak             = false;
                    eyeClosedY         = false;
                    baselineReady       = false;
                    eyeCalXY            = zeros(0, 2);
                    openBuf(:)          = NaN;
                    headMoveCooldown    = 0;
                    upperFracBuf       = ones(1, 3) * 0.5;
                    closureBuf(:)       = 0;
                    lastCentBlinkTime   = -Inf;
                end
            else
                noEyeCount      = 0;
                eyePresentCount = eyePresentCount + 1;
                if eyePresentCount >= eyePresentLimit
                    eyePresent      = true;
                    eyePresentCount = eyePresentLimit;
                end
            end

            % (The LDR / temperature / heart readouts used to be drawn here, in
            %  the frame path. They now live in the SENSOR DISPLAY REFRESH block
            %  near the top of the loop so they keep updating with no camera
            %  attached — that dependency was the main host-side coupling bug.)

            if eyePresent
                set(hWearText, 'String', 'EYE DETECTED', 'Color', 'green');
                if isHeadMove
                    set(hHeadText, 'String', 'HEAD MOVE', 'Color', [1 0.45 0]);
                elseif headMoveCooldown > 0
                    set(hHeadText, 'String', sprintf('settling %d', headMoveCooldown), 'Color', [1 0.7 0.2]);
                else
                    set(hHeadText, 'String', '');
                end
            else
                set(hWearText, 'String', 'NO EYE - paused', 'Color', 'red');
                title(ax, 'SEARCHING...', 'Color', 'y');
                updateTimer(hTimer, elapsedTotal);
                drawnow; continue
            end

            % =============================================================
            % FRAME DIFFERENCE (split for head-move ratio)
            % =============================================================
            % Split at the eye's own row (rolling baseline), not the fixed
            % middle: as the glasses slip the eye moves in the frame, and a
            % fixed split started classing ordinary blinks as head moves.
            if baselineReady, splitRow = min(max(round(centYbaseline), 15), roiSize - 15); end
            upperReg  = abs(double(eyeImg(1:splitRow,:))     - double(prevFrame(1:splitRow,:)));
            lowerReg  = abs(double(eyeImg(splitRow+1:end,:)) - double(prevFrame(splitRow+1:end,:)));
            upperDiff = mean(upperReg(:));
            lowerDiff = mean(lowerReg(:));
            frameDiff  = (upperDiff + lowerDiff) / 2;
            prevFrame  = eyeImg;
            normDiff   = min(frameDiff / 50, 1.0);
            diffBuffer = [diffBuffer(2:end), normDiff];
            smoothDiff = mean(diffBuffer);

            % =============================================================
            % EYE TRACKER (V7, replaces the dark-pixel / dark-spot centroid)
            % =============================================================
            % The eye = the strongest DARK ROUND BLOB at any size: the whole
            % iris in visible light, only the pupil under IR (the iris turns
            % light in IR, which made the old iris/centroid trackers lock onto
            % lashes and lids). Scale-normalised Laplacian of Gaussian, sigma
            % 2.5..11 px, on the raw frame / its median (exposure cancels).
            % Searched only within EYE_WIN px of the eye centre, which is
            % calibrated at START and then follows the median open-eye
            % position of the last ~3 s -- an adaptive "outline" of the eye.
            %
            % EYE CLOSURE % = how much that blob has faded vs the open eye's
            % recent strength: a closing lid puts skin over the pupil/iris.
            % Replayed on the 22:15 recording (7246 frames, 24 fps): 71 blinks
            % (14/min, median 0.16 s, max 1.7 s); none of the IR-lit open frames
            % read as closed; tracked position jitter median 1 px/frame.
            % The live recording had 102 "centroid" closures and 27 motion
            % blinks, with the marker on the lid at the top of the image.
            eyeCalibrated = baselineReady;
            if eyeCalibrated, eyeWin = hypot(eyeXX - centXbaseline, eyeYY - centYbaseline) <= EYE_WIN; else, eyeWin = []; end
            [blobS, blobXY, blobR] = blobScore(imgaussfilt(rawImg, 1), eyeWin);
            pupOk = isfinite(blobS);
            if pupOk
                centXsmooth = centSmAlpha * blobXY(1) + (1 - centSmAlpha) * centXsmooth;
                centYsmooth = centSmAlpha * blobXY(2) + (1 - centSmAlpha) * centYsmooth;
                gazeBuf = [gazeBuf(2:end, :); blobXY];
            end
            gazeXY = median(gazeBuf, 1, 'omitnan');
            openRef = median(openBuf, 'omitnan');
            if isnan(openRef) || ~pupOk, closurePct = 0; else, closurePct = 100 * (1 - blobS / openRef); end
            closureBuf = [closureBuf(2:end), closurePct];
            spotOk = pupOk && ~eyeClosedY;
            centYamp = closurePct;                         % plotted green line + logged
            th = linspace(0, 2 * pi, 40);
            if pupOk
                set(hIrisCircle, 'XData', blobXY(1) + blobR * cos(th), 'YData', blobXY(2) + blobR * sin(th), ...
                    'Color', ifelseColor(eyeClosedY));
            end
            set(hCentRaw, 'XData', gazeXY(1), 'YData', gazeXY(2), 'Visible', onOff(spotOk));

            % =============================================================
            % EYE TEMPLATES -- does the frame LOOK closed? (Eye calibrate)
            % =============================================================
            % Closure % is relative to the last ~3 s, so it detects a lid
            % CLOSING but cannot hold a long closure: on the 00:23 run an 8 s
            % closure read under the line after ~1 s (the lash line under the
            % lid is itself a weak dark blob, and the reference catches up).
            % A frame-vs-template comparison is absolute: closer to the
            % closed-eye template than the open one = closed. On that run,
            % with 3 s of each: 100% of closed frames and 90-98% of open
            % frames classified right. It only HOLDS a closure open (ends
            % it at the real re-opening); onset still comes from closure %.
            xv = rawImg(:); xv = (xv - mean(xv)) / max(std(xv), 1);
            if eyeCalReq
                eyeCalReq = false; tplPhase = 1; tplT0 = now; tplAcc(:) = 0; tplN = 0;
                tplOpenAcc = []; beep;
                fprintf('Eye calibrate: keep eyes OPEN 3 s...\n');
            end
            if tplPhase > 0
                el = now - tplT0;
                if el >= 0.5 && el <= 3.5, tplAcc = tplAcc + xv; tplN = tplN + 1; end
                if tplPhase == 1
                    set(hStateText, 'String', 'EYE CAL: eyes OPEN', 'Color', 'yellow');
                    if el > 3.5
                        tplOpenAcc = tplAcc / max(tplN, 1); tplPhase = 2; tplT0 = now;
                        tplAcc(:) = 0; tplN = 0; beep;
                        stimPending(end+1, :) = {datetime('now'), 'eye cal (closed)'};
                        fprintf('Eye calibrate: CLOSE eyes until the double beep...\n');
                    end
                else
                    set(hStateText, 'String', 'EYE CAL: eyes CLOSED', 'Color', 'yellow');
                    if el > 4
                        tc = tplAcc / max(tplN, 1); tplPhase = 0;
                        r = corr(tc, tplOpenAcc);
                        if tplN < 20 || r > 0.9
                            fprintf('Eye calibrate FAILED (open and closed look the same, r=%.2f): try again\n', r);
                        else
                            tplOpen = tplOpenAcc; tplClosed = tc; tplBuf(:) = 1;   % eyes are still shut
                            fprintf('Eye calibrate OK (open vs closed r=%.2f)\n', r);
                        end
                        beep; pause(0.15); beep;
                    end
                end
            end
            if ~isempty(tplOpen)
                tplBuf = [tplBuf(2:end), (tplClosed - tplOpen).' * xv / numel(xv)];
                looksClosed = median(tplBuf) > 0;
                if ~looksClosed && ~eyeClosedY             % follow slow glasses slip (~8 s)
                    tplOpen = 0.995 * tplOpen + 0.005 * xv;
                elseif looksClosed && eyeClosedY && now - lidCloseTime > 1
                    tplClosed = 0.995 * tplClosed + 0.005 * xv;
                end
            else
                looksClosed = false;
            end
            looksClosed = looksClosed || tplPhase == 2;   % told to keep them shut: hold that closure too
            if ~eyeClosedY || looksClosed, tplOpenSince = NaN;
            elseif isnan(tplOpenSince), tplOpenSince = now; end

            % =============================================================
            % HEAD MOVEMENT DETECTION
            % =============================================================
            totalMotion = upperDiff + lowerDiff;
            upperFrac   = upperDiff / (totalMotion + 1e-6);
            upperFracBuf = [upperFracBuf(2:end), upperFrac];
            smoothUpperFrac = mean(upperFracBuf);

            isHeadMove = (smoothUpperFrac < headMoveUpperFracThresh && ...
                          totalMotion > max(headMoveMinMotion, 100 * (motionFloor + 0.05)));

            if isHeadMove
                headMoveCooldown = headMoveCooldownFrames;
            elseif headMoveCooldown > 0
                headMoveCooldown = headMoveCooldown - 1;
            end
            suppressDetection = isHeadMove || (headMoveCooldown > 0);

            % =============================================================
            % EYE CALIBRATION (V7) -- first EYE_CAL_N frames with a visible eye
            % then continuously drift via slow EMA (rolling baseline).
            % =============================================================
            if ~baselineReady
                % CALIBRATION: where is the eye? Strongest blob anywhere,
                % EYE_CAL_N frames, median position -> the search area.
                if pupOk
                    eyeCalXY(end+1, :) = blobXY; %#ok<AGROW>
                    openIdx = mod(openIdx, numel(openBuf)) + 1; openBuf(openIdx) = blobS;
                end
                if size(eyeCalXY, 1) >= EYE_CAL_N
                    c0 = median(eyeCalXY, 1);
                    centXbaseline = c0(1); centYbaseline = c0(2);
                    eyePosBuf(:) = NaN; eyePosBuf(1, :) = c0; eyePosIdx = 1;
                    baselineReady = true; eyeCalXY = zeros(0, 2);
                    fprintf('Eye calibrated at x=%.0f y=%.0f (open blob strength %.2f)\n', c0, median(openBuf, 'omitnan'));
                end
                set(hStateText, 'String', 'CALIBRATING EYE...', 'Color', 'yellow');
                centYlog(end+1)    = centYsmooth;
                centYampLog(end+1) = 0;
                baseYlog(end+1)    = 0;
                timeLog(end+1)     = now;
                diffLog(end+1)     = smoothDiff;
                centXlog(end+1)    = centXsmooth; %#ok<AGROW>
                suppressLog(end+1) = suppressDetection; %#ok<AGROW>
                pupilXlog(end+1) = NaN; pupilYlog(end+1) = NaN; %#ok<AGROW>
                updateTimer(hTimer, elapsedTotal);
                drawnow limitrate;
                continue
            end

            % Manual reset
            if ~isempty(resetBaseline) && resetBaseline
                if eyeClosedY, fprintf('  (open closure ended by the reset, duration discarded)\n'); end
                eyeClosedY          = false;
                closedBelowSince    = NaN;
                closureBuf(:)       = 0; openBuf(:) = NaN; eyeCalXY = zeros(0, 2);
                set(hStateText, 'String', 'RESETTING...', 'Color', 'yellow');
                baselineReady       = false;
                resetBaseline       = false;
                fprintf('Eye tracker reset - recalibrating...\n');
            end

            % --- OPEN REFERENCE + EYE CENTRE ---
            % Reference = median blob strength of the last ~3 s, ALL frames.
            % Blinks are too short to move a median, and an ungated median
            % follows lighting changes (IR vs visible) by itself. Gating it on
            % "eye open" deadlocked: replaying the 22:15 run, a light change
            % made the eye read closed, the reference stopped updating, and
            % one "blink" lasted 24 s.
            if pupOk
                openIdx = mod(openIdx, numel(openBuf)) + 1; openBuf(openIdx) = blobS;
            end
            if pupOk && ~eyeClosedY && ~suppressDetection
                eyePosIdx = mod(eyePosIdx, size(eyePosBuf, 1)) + 1; eyePosBuf(eyePosIdx, :) = blobXY;
                c0 = median(eyePosBuf, 1, 'omitnan'); centXbaseline = c0(1); centYbaseline = c0(2);
            end
            exc = [centXsmooth - centXbaseline, centYsmooth - centYbaseline];   % for closeDir bookkeeping
            set(hBaseLine, 'XData', [1 roiSize], 'YData', [centYbaseline centYbaseline]);

            centYlog(end+1)     = blobXY(2);         % tracked eye y (raw)
            centYampLog(end+1)  = closurePct;        % eye closure %, drives the detector
            baseYlog(end+1)     = 0;                 % open eye = 0 %
            centDevInstant = closurePct;
            centDevMed     = median(closureBuf);
            centDev        = max(centDevInstant, 0);

            % =============================================================
            % CENTROID CLOSURE DETECTOR -- authoritative OPEN/CLOSED + DURATION
            % =============================================================
            if eyeClosedY && centDevMed < closedDeltaPx
                if isnan(closedBelowSince), closedBelowSince = now; end
            else
                closedBelowSince = NaN;
            end
            % Head movement blocks only the START of a closure (it can mimic
            % one). Ending is checked regardless: frozen during a head move,
            % a real blink stayed "closed" and came out ~3 s long.
            if ~suppressDetection && ~eyeClosedY && centDevInstant > closedDeltaPx && ...
                    (now - lastCentBlinkTime) > centMinGap
                eyeClosedY    = true;
                lidCloseTime  = now;
                lastCentBlinkTime = now;
                centBlinkCount             = centBlinkCount + 1;
                centBlinkTimestamps(end+1) = now;
                centBlinksThisMin          = centBlinksThisMin + 1;
                tickLines(hCentMarks, centBlinkTimestamps, now - 60, 100);
                set(hStateText, 'String', 'EYE CLOSED', 'Color', 'c');
                fprintf('Closure start @ %.2fs (centroid blink #%d)\n', now, centBlinkCount);
            elseif eyeClosedY && now - lidCloseTime > MAX_CLOSED
                % Nothing holds an eye shut this long in these tests: the
                % open position moved while the baseline was frozen.
                fprintf('Closed > %d s: tracker assumed lost, recalibrating (duration discarded)\n', MAX_CLOSED);
                resetBaseline = true;
                if ~isempty(tplOpen), fprintf('  eye templates cleared too: press Eye calibrate again\n'); end
                tplOpen = []; tplClosed = [];
            elseif eyeClosedY && ~looksClosed && (centDevMed < openMarginPx || ...
                    (centDevMed < closedDeltaPx && ~isnan(closedBelowSince) && ...
                     now - closedBelowSince >= REOPEN_HOLD))
                % OPEN when green is back near the baseline, OR has stayed
                % under the red line for REOPEN_HOLD s. The second rule is
                % the stuck-marker fix: the baseline is frozen while
                % closed, so if the open eye settles a few px lower (gaze
                % down, glasses slip, low slider setting) green never got
                % back within openMarginPx and one blink lasted seconds.
                % That closure ends when green first went under the line.
                endT = now;
                if centDevMed >= openMarginPx, endT = closedBelowSince; end
                if ~isempty(tplOpen), endT = max(endT, tplOpenSince); end   % held by the templates
                eyeClosedY = false; closedBelowSince = NaN;
                set(hStateText, 'String', 'EYE OPEN', 'Color', 'green');
                duration   = endT - lidCloseTime;
                if duration >= 0.03 && duration <= 30.0
                    blinkDurations(end+1)   = duration;
                    durationsThisMin(end+1) = duration;
                    % Keep only the labels still on screen: one text object
                    % per blink, never deleted, made every redraw slower
                    % over a long session.
                    hDurLabels(end+1) = text(axCent, now, 95, sprintf('%.2fs', duration), ...
                        'Color', 'c', 'FontSize', 6, 'HorizontalAlignment', 'center'); %#ok<AGROW>
                    old = arrayfun(@(h) h.Position(1) < now - 60, hDurLabels);
                    delete(hDurLabels(old)); hDurLabels(old) = [];
                    fprintf('  closure end   @ %.2fs  dur=%.3fs\n', now, duration);
                else
                    fprintf('  closure end   @ %.2fs  dur OUT OF RANGE %.3fs (discarded)\n', now, duration);
                end
            end

            % =============================================================
            % MOTION PEAK -- independent blink counter (MOTION method).
            % =============================================================
            timeSinceLastBlink  = now - lastBlinkTime;
            motionPeakCompleted = false;
            % Same margins as designed (0.13 above / release 45% of the way
            % up), measured from the floor instead of from zero.
            effThr  = max(peakThreshold, motionFloor + (peakThreshold - 0.05));
            release = motionFloor + 0.45 * (effThr - motionFloor);
            if ~inPeak
                motionFloor = 0.98 * motionFloor + 0.02 * min(smoothDiff, effThr);   % peaks clipped out
            elseif now - peakStart > 1.5
                % A "peak" this long is a raised noise level, not a blink:
                % drop it uncounted and let the floor catch up.
                inPeak = false; motionFloor = 0.9 * motionFloor + 0.1 * smoothDiff;
            end
            if abs(get(hThreshLine, 'Value') - effThr) > 0.005, set(hThreshLine, 'Value', effThr); end
            if ~inPeak && smoothDiff > effThr && timeSinceLastBlink > minBlinkGap
                inPeak = true; peakStart = now;
                peakVec = exc;
            elseif inPeak && smoothDiff < release
                inPeak = false;
                motionPeakCompleted = true;
            end
            if inPeak && norm(exc) > norm(peakVec), peakVec = exc; end

            % A peak the eye tracker saw close the eye is a blink even if the
            % head-move flag came on during it: on the 00:23 run the flag
            % rose mid-peak on ~30 real blinks (each with a closure) and
            % dropped them (23 counted of ~55).
            if motionPeakCompleted && timeSinceLastBlink > minBlinkGap && ...
                    (~suppressDetection || lidCloseTime >= peakStart - 0.15)
                blinkCount = blinkCount + 1;
                interval   = now - lastBlinkTime;
                if blinkCount > 1
                    blinkIntervals(end+1)   = interval;
                    intervalsThisMin(end+1) = interval;
                end
                lastBlinkTime          = now;
                blinkTimestamps(end+1) = now;
                blinksThisMin          = blinksThisMin + 1;

                % Learn the closing direction from this blink's excursion.
                if norm(peakVec) >= 3
                    closeDirBuf = [closeDirBuf(max(1, end-18):end, :); peakVec / norm(peakVec)];
                    newDir = closeDirFrom(closeDirBuf);
                    if acosd(max(-1, min(1, newDir * closeDir.'))) > 5
                        fprintf('Closing direction -> %.0f deg from straight down\n', ...
                                atan2d(newDir(1), newDir(2)));
                    end
                    closeDir = newDir;
                end

                tickLines(hBlinkMarks, blinkTimestamps, now - 60, 1.1);
                fprintf('Motion blink #%d @ %.2fs\n', blinkCount, now);
            end

            % --- LOG & PLOTS ---
            timeLog(end+1) = now;
            diffLog(end+1) = smoothDiff;
            centXlog(end+1)    = centXsmooth; %#ok<AGROW>
            suppressLog(end+1) = suppressDetection; %#ok<AGROW>
            % Gaze uses the pupil only while the eye is open and the head still.
            if spotOk && ~eyeClosedY && ~suppressDetection, gXY = gazeXY; else, gXY = [NaN NaN]; end
            pupilXlog(end+1) = gXY(1); pupilYlog(end+1) = gXY(2); %#ok<AGROW>
            if now > 60
                xlim(axBlink, [now-60, now]);
                xlim(axCent,  [now-60, now]);
            end
            % PLOT ONLY THE VISIBLE TAIL.
            %
            % These five lines were redrawn from the FULL logs every frame, and
            % those logs grow forever — one point per frame, unbounded. After a
            % few minutes that is thousands of points per line, five lines, every
            % single frame, while the axes only ever show the last 60 s. The rest
            % was drawn off-screen and discarded.
            %
            % That cost is why the heart channel starved: MATLAB handles ONE
            % packet per loop iteration, the frame path is by far the most
            % expensive iteration, and it got steadily more expensive as the logs
            % grew. Measured effect: 310 heart samples received in 3m40s out of
            % ~22000 sent, with the survivors arriving as misaligned garbage
            % because the backlog forced flushes and resyncs.
            %
            % Slicing bounds the per-frame graphics cost to a constant. The FULL
            % arrays are untouched — session saving still gets everything.
            if now - lastUiDraw >= UI_PERIOD    % V7: redraw ~10x/s, track every frame
                lastUiDraw = now;
                nVis = numel(timeLog);
                vis  = max(1, nVis - PLOT_TAIL + 1) : nVis;
                set(hDiffLine,   'XData', timeLog(vis), 'YData', diffLog(vis));
                set(hCentAmpLog, 'XData', timeLog(vis), 'YData', centYampLog(vis));
                set(hBasePlot,   'XData', timeLog(vis), 'YData', baseYlog(vis));
                set(hMotionCount, 'String', sprintf('MOTION blinks: %d',   blinkCount));
                set(hCentCount,   'String', sprintf('CENTROID blinks: %d', centBlinkCount));
                set(hClosedHi,   'XData', timeLog(vis), 'YData', baseYlog(vis) + closedDeltaPx);
                set(hClosedLo,   'XData', timeLog(vis), 'YData', baseYlog(vis) - closedDeltaPx);

                % --- STATS BAR ---
                elapsed = max(now, 0.001);
                bpm = blinkCount / (elapsed / 60);
                if bpm > 25,    fState = 'TIRED';   fColor = 'r';
                elseif bpm < 6, fState = 'FOCUSED'; fColor = 'y';
                else,           fState = 'NORMAL';  fColor = 'g';
                end

                avgDur = 0; maxDur = 0;
                if ~isempty(blinkDurations)
                    avgDur = mean(blinkDurations);
                    maxDur = max(blinkDurations);
                end

                % Gaze over the last 60 s, refreshed every 8 frames.
                if mod(numel(timeLog), 8) == 0
                    kG = timeLog > now - 60;
                    Gl = GazeMetrics(timeLog(kG), pupilXlog(kG), pupilYlog(kG));
                    if isnan(Gl.rate)
                        gazeStr = 'Gaze: tracking...';
                    else
                        gazeStr = sprintf('Gaze: %.0f shifts/min, fixation %.1f s', Gl.rate, Gl.fix);
                    end
                    set(hAlphaText, 'String', gazeStr);
                end

                if eyeClosedY
                    lidStateStr = 'CLOSED';
                else
                    lidStateStr = 'open';
                end
                title(axCent, sprintf('Closure %.0f%%  |  eye at (%.0f, %.0f)  |  %s', ...
                    centYamp, centXbaseline, centYbaseline, lidStateStr), 'Color', 'w', 'FontSize', 8);

                % V4: light state and which threshold set it selected, so a
                % misclassification is visible in the same glance as the tracking.
                if lightState == 0
                    lightStr = '--- (no LDR)';
                else
                    lightStr = sprintf('%s/%d', lightNames{lightState}, round(ldrVal));
                end
                if useDarkThresholds, threshStr = 'IR/DARK'; else, threshStr = 'NORMAL'; end

                % V5: temperature and PPG state on the same bar, so one glance
                % covers every channel.
                if isnan(tempObj)
                    tempStr = '--';
                else
                    tempStr = sprintf('%.1f/%.1fC', tempObj, tempAmb);
                end
                if nHeart == 0
                    heartStr = 'none';
                else
                    heartStr = sprintf('%d', nHeart);
                end
                % Frame brightness on the bar too: the single number that says
                % whether a dark image is an exposure problem or a dead sensor.
                camStr = sprintf('%.1f', lastFrameMean);

                set(hStats, 'String', sprintf( ...
                    'Motion: %d | Centroid: %d | BPM: %.1f | AvgDur: %.3fs | MaxDur: %.3fs | Amp: %.1f | Base: %.1f | Dev: %.1fpx | %s | Eye: %s | %s | Light: %s | Thresh: %s | Temp obj/amb: %s | PPG samples: %s | FrameMean: %s | %s', ...
                    blinkCount, centBlinkCount, bpm, avgDur, maxDur, centYamp, centYbaseline, centDev, gazeStr, lidStateStr, fState, lightStr, threshStr, tempStr, heartStr, camStr, ...
                    decodeStatus(statusMask, lastStatTime > -Inf)));
                title(axBlink, sprintf('Blinks - %s   %d blinks  %.1f BPM', ...
                      fState, blinkCount, bpm), ...
                      'Color', fColor, 'FontSize', 9, 'FontWeight', 'bold');

                updateTimer(hTimer, elapsedTotal);
            end
            drawnow limitrate;

            if saveTrigger
                saveSession(saveFolder, sessionTag, blinkCount, blinkTimestamps, ...
                            blinkIntervals, blinkDurations, minBinCounts, ...
                            minBinAvgInterval, minBinAvgDur, minBinMaxDur, minBinGazeShifts, ...
                            centBlinkCount, centBlinkTimestamps, minBinCentCounts, ...
                            timeLog, diffLog, centYlog, centYampLog, baseYlog, ...
                            ldrTimeLog, ldrValLog, ldrStateLog, ...
                            heartTimeLog(1:nHeartLog), heartFiltLog(1:nHeartLog), ...
                            heartDerivLog(1:nHeartLog), ...
                            tempTimeLog, tempAmbLog, tempObjLog, fig, eyeImg, '_manual', ...
                            struct('beatTimes', beatTimes, 'bpmTimeLog', bpmTimeLog, ...
                                   'bpmValLog', bpmValLog, 'minBinBpm', minBinBpm, ...
                                   'minBinTempObj', minBinTempObj, ...
                                   'minBinTempAmb', minBinTempAmb, ...
                                   'sessionStartWall', sessionStartWall, ...
                                   'ldrCalLog', ldrCalLog, 'centXlog', centXlog, ...
                                   'suppressLog', suppressLog, 'pauseLog', pauseLog, ...
                                   'closeDir', closeDir, 'closedLog', closedLog, ...
                           'stimTimes', stimTimes, 'stimNames', {stimNames}, ...
                           'pupilXlog', pupilXlog, 'pupilYlog', pupilYlog, ...
                           'minBinFixDur', minBinFixDur, 'minBinGazeMove', minBinGazeMove, ...
                           'exposureLog', exposureLog));
                saveTrigger = false;
            end
        end

        if ~ishandle(fig), exitReason = 'closed_early'; end

    catch ME
        % FULL report, not just ME.message. The message alone ("Index exceeds
        % array bounds") names no file and no line, which makes a mid-session
        % crash unactionable — you get told it broke and nothing about where.
        % getReport gives the stack. The saved session below still runs, so a
        % crash never costs the data collected up to that point.
        fprintf(2, '\n=== CRASH at %s ===\n', datestr(now, 'HH:MM:SS'));
        fprintf(2, '%s\n', getReport(ME, 'extended', 'hyperlinks', 'off'));
        fprintf(2, 'State: frames=%d heartSamples=%d rxBuf=%d bytes\n', ...
                numel(timeLog), nHeartLog, numel(rxBuf));
        fprintf(2, '=== data up to here is still saved below ===\n\n');
        exitReason = 'error';
    end

    % Flush last partial minute
    mT0 = (minuteBin - 1) * 60;
    mT1 = minuteBin * 60;
    minBinBpm(end+1)     = meanInWindow(bpmTimeLog,  bpmValLog,  mT0, mT1);
    minBinTempObj(end+1) = meanInWindow(tempTimeLog, tempObjLog, mT0, mT1);
    minBinTempAmb(end+1) = meanInWindow(tempTimeLog, tempAmbLog, mT0, mT1);
    minBinCounts(end+1)     = blinksThisMin;
    minBinCentCounts(end+1) = centBlinksThisMin;
    if ~isempty(intervalsThisMin)
        minBinAvgInterval(end+1) = mean(intervalsThisMin);
    else
        minBinAvgInterval(end+1) = NaN;
    end
    if ~isempty(durationsThisMin)
        minBinAvgDur(end+1) = mean(durationsThisMin);
        minBinMaxDur(end+1) = max(durationsThisMin);
    else
        minBinAvgDur(end+1) = NaN;
        minBinMaxDur(end+1) = NaN;
    end
    kG = timeLog >= (minuteBin - 1) * 60;
    G = GazeMetrics(timeLog(kG), pupilXlog(kG), pupilYlog(kG));
    minBinGazeShifts(end+1) = G.rate; minBinFixDur(end+1) = G.fix; minBinGazeMove(end+1) = G.moveMean;

    % Final refresh so the trends window matches the saved session, including
    % the partial last minute flushed just above.
    minuteStruct = buildMinuteStruct(minBinCounts, minBinCentCounts, minBinAvgDur, ...
                                     minBinMaxDur, minBinGazeShifts, minBinFixDur, minBinBpm, ...
                                     minBinTempObj, minBinTempAmb);
    if ishandle(figMin)
        PerMinutePanels(figMin, minuteStruct, ...
                        sprintf('Per-Minute Trends — %s', sessionTag));
        try
            exportgraphics(figMin, fullfile(saveFolder, ...
                sprintf('session_%s_perminute.png', sessionTag)), 'Resolution', 150, 'BackgroundColor', [0.08 0.08 0.08]);
        catch, fprintf('  (per-minute PNG skipped)\n'); end
    end

    if fidFrames > 0, fclose(fidFrames); end
    fprintf('\n=== SAVING (reason: %s) ===\n', exitReason);
    try
        saveSession(saveFolder, sessionTag, blinkCount, blinkTimestamps, ...
                    blinkIntervals, blinkDurations, minBinCounts, ...
                    minBinAvgInterval, minBinAvgDur, minBinMaxDur, minBinGazeShifts, ...
                    centBlinkCount, centBlinkTimestamps, minBinCentCounts, ...
                    timeLog, diffLog, centYlog, centYampLog, baseYlog, ...
                    ldrTimeLog, ldrValLog, ldrStateLog, ...
                    heartTimeLog(1:nHeartLog), heartFiltLog(1:nHeartLog), ...
                    heartDerivLog(1:nHeartLog), ...
                    tempTimeLog, tempAmbLog, tempObjLog, fig, eyeImg, ['_' exitReason], ...
                    struct('beatTimes', beatTimes, 'bpmTimeLog', bpmTimeLog, ...
                           'bpmValLog', bpmValLog, 'minBinBpm', minBinBpm, ...
                           'minBinTempObj', minBinTempObj, ...
                           'minBinTempAmb', minBinTempAmb, ...
                           'sessionStartWall', sessionStartWall, ...
                           'ldrCalLog', ldrCalLog, 'centXlog', centXlog, ...
                           'suppressLog', suppressLog, 'pauseLog', pauseLog, ...
                           'closeDir', closeDir, 'closedLog', closedLog, ...
                           'stimTimes', stimTimes, 'stimNames', {stimNames}, ...
                           'pupilXlog', pupilXlog, 'pupilYlog', pupilYlog, ...
                           'minBinFixDur', minBinFixDur, 'minBinGazeMove', minBinGazeMove, ...
                           'exposureLog', exposureLog));
        fprintf('=== SAVE COMPLETE ===\n');
        if ishandle(fig)
            set(fig, 'Name', 'Blink Detector V6 - DATA SAVED');
            if ishandle(hTimer)
                set(hTimer, 'String', 'SESSION COMPLETE - DATA SAVED', 'ForegroundColor', 'g');
            end
            drawnow;
        end
    catch saveErr
        fprintf('SAVE FAILED: %s\n', saveErr.message);
    end

    % Leave the LEDs off. Without this the next session inherits them on,
    % which is exactly how the same room came to read ~700 one run and ~70
    % the next. Belt and braces with the 0xF0 sent at startup.
    try, write(s, uint8(0xF0), 'uint8'); pause(0.05); catch, end
    try, delete(s); catch, end
    fprintf('Session ended (IR LEDs commanded OFF).\n');

    % Offered only after a normal STOP / window close, never after an error.
    if any(strcmp(exitReason, {'stopped', 'closed_early'})) && offerDelete(saveFolder) && ishandle(hTimer)
        set(hTimer, 'String', 'DATA DELETED (in Trash)', 'ForegroundColor', [1 0.5 0.4]);
    end
end

function deleted = offerDelete(folder)
    % The data is already saved. Deleting is only ever the user's own choice,
    % confirmed twice, and goes to the Trash so a wrong click can be undone.
    deleted = false;
    [~, name] = fileparts(folder);
    if ~strcmp(questdlg(sprintf('Recording saved in:\n%s\n\nKeep this data or delete it?', folder), ...
                        'Recording finished', 'Keep', 'Delete', 'Keep'), 'Delete')
        return;
    end
    if ~strcmp(questdlg(sprintf(['Are you sure you want to DELETE this recording?\n\n%s\n\n' ...
                                 'The whole session folder will be moved to the Trash.'], name), ...
                        'Delete recording', 'Delete', 'Cancel', 'Cancel'), 'Delete')
        fprintf('Delete cancelled -- data kept in %s\n', folder);
        return;
    end
    [ok, msg] = movefile(folder, fullfile(getenv('HOME'), '.Trash', name));
    if ok
        deleted = true;
        fprintf('Session %s moved to the Trash.\n', name);
    else
        warndlg(sprintf('Could not delete the recording (%s).\nIt is still in:\n%s', msg, folder), ...
                'Delete recording');
    end
end

% ============= TIMER =============
function updateTimer(hTimer, elapsed)
    if ~ishandle(hTimer), return; end
    set(hTimer, 'String', sprintf('REC %02d:%02d:%02d', floor(elapsed/3600), ...
        floor(mod(elapsed, 3600)/60), floor(mod(elapsed, 60))));
end

function tickLines(h, tb, tMin, yTop)
    % Vertical blink ticks for the visible minute only. Appending every tick
    % for the whole session made this line (and each redraw) grow forever.
    tb = tb(tb >= tMin);
    set(h, 'XData', reshape([tb; tb; nan(size(tb))], 1, []), ...
           'YData', repmat([0 yTop NaN], 1, numel(tb)));
end

function sendCamera(s, frameMs, aec, gain)
    % V7 firmware commands: 0xF4 ms, 0xF5 hi lo, 0xF6 gain. An exposure whose
    % low byte is 0xF0-0xF6 is nudged off it: V6 firmware ignores 0xF5 but
    % would read such a byte as an IR-LED / command byte.
    if mod(aec, 256) >= 240 && mod(aec, 256) <= 246, aec = aec - 7; end
    b = [];
    if ~isempty(frameMs), b = [b, 244, frameMs]; end
    b = [b, 245, floor(aec / 256), mod(aec, 256), 246, gain];
    try, write(s, uint8(b), 'uint8'); catch, end
end

function [aec, gain] = nextExposure(aec, gain, ratio, maxGain)
    % One closed-loop step: ratio = measured highlights / target. Dead band
    % +/-8%; exposure moves <=10% per step (so the tracker's brightness
    % normalisation follows smoothly); gain only once exposure is at a limit.
    % Exposure is capped at 400: measured on the board, 400..1200 all give
    % the same image (the sensor cannot expose longer than one frame), while
    % gain 2 -> 6 at exposure 300 raised the mean 99 -> 163.
    if ratio > 0.92 && ratio < 1.08, return; end
    if ratio <= 0.92                               % too dark
        if aec < 400, aec = min(400, round(aec * min(1.10, 1 / ratio)));
        elseif gain < maxGain, gain = gain + 1; end
    else                                           % too bright
        if gain > 2, gain = gain - 1;
        else, aec = max(20, round(aec * max(0.90, 1 / ratio))); end
    end
end

function p = glassesPort(p)
    % The configured port if present, else the first USB modem port (the
    % name can change after a replug). Stays as configured if none is found,
    % so the error message names the expected port.
    try
        av = serialportlist("all");
        if any(av == p), return; end
        m = av(contains(av, "usbmodem"));
        if ~isempty(m), fprintf('Port %s not found, using %s\n', p, m(1)); p = m(1); end
    catch
    end
end

function s = resetBoard(s, port, baud)
    % ESP32-S3 USB reset, the sequence esptool/Arduino use for a normal
    % (not download-mode) reset: RTS pulse with DTR held low.
    try, setDTR(s, false); setRTS(s, true); pause(0.15); setRTS(s, false); catch, end
    try, delete(s); catch, end
    s = [];
    t = tic;
    pause(1.5);                                     % USB re-enumerates
    while toc(t) < 12
        try
            s = serialport(glassesPort(port), baud);
            flush(s);
            return;
        catch
            pause(0.5);
        end
    end
    fprintf('Board did not come back after the reset -- unplug and replug it.\n');
end

function keepAlive(s)
    % V7 firmware sends frames/heart only to a host it has heard from in the
    % last 3 s (its own USB "connected" flag proved unreliable). 0xF7 = no-op.
    persistent last
    if isempty(last) || toc(last) > 0.5
        try, write(s, uint8(247), 'uint8'); catch, end
        last = tic;
    end
end

function closeAllPorts()
    try, delete(serialportfind); catch, end
end

function requestEyeCal()
    global eyeCalReq;
    eyeCalReq = true;
end

function requestMarkerReset()
    global resetBaseline;
    resetBaseline = true;
end

function markStimulus(hEdit)
    global stimPending;
    name = strtrim(get(hEdit, 'String'));
    if isempty(name), name = 'stimulus'; end
    stimPending(end+1, :) = {datetime('now'), name};
end

function c = ifelseColor(closed)
    if closed, c = [0 1 1]; else, c = [0.2 1 0.2]; end
end

function [score, xy, rad] = blobScore(I, win)
    % Strongest dark round blob, any size: scale-normalised Laplacian of
    % Gaussian over sigma 2.5..11 (pupil r~4 .. iris r~16), on the image
    % divided by its median so exposure/gain changes cancel. win = logical
    % mask of allowed centres ([] = anywhere). score = -Inf if win is empty.
    persistent K sig
    if isempty(K)
        sig = [2.5 3.5 5 7 9 11];
        for k = 1:numel(sig), K{k} = sig(k)^2 * fspecial('log', 2 * ceil(3 * sig(k)) + 1, sig(k)); end
    end
    J = I / max(median(I(:)), 1);
    score = -Inf; xy = [NaN NaN]; rad = NaN;
    for k = 1:numel(sig)
        Rk = imfilter(J, K{k}, 'replicate');              % dark blob -> positive response
        Rk([1:4 end-3:end], :) = -Inf; Rk(:, [1:4 end-3:end]) = -Inf;
        if ~isempty(win), Rk(~win) = -Inf; end
        [v, i] = max(Rk(:));
        if v > score, score = v; [r, c] = ind2sub(size(Rk), i); xy = [c r]; rad = sqrt(2) * sig(k); end
    end
end

function s = onOff(tf)
    if tf, s = 'on'; else, s = 'off'; end
end

function setClosedPx(src)
    global userClosedPx;
    userClosedPx = round(get(src, 'Value'));   % V7: % closure, 1 % steps
end

function startRecording(src, ~)
    global startReq;
    startReq = true;
    set(src, 'Enable', 'off');
end

function togglePause(src, ~)
    global pauseReq;
    pauseReq = ~pauseReq;
    if pauseReq
        set(src, 'String', 'RESUME', 'BackgroundColor', [0.08 0.6 0.9]);
    else
        set(src, 'String', 'PAUSE', 'BackgroundColor', [0.85 0.65 0.1]);
    end
end

function d = closeDirFrom(U)
    % Closing direction from recent blinks' unit excursion vectors (rows,
    % image x/y with y DOWN). Stays straight down until 5 blinks AGREE (mean
    % vector length >= 0.6) and the result is within 75 deg of down -- gaze
    % shifts that happen to coincide with a blink must not steer it sideways.
    d = [0 1];
    if size(U, 1) < 5, return; end
    m = mean(U, 1);
    if norm(m) < 0.6, return; end
    m = m / norm(m);
    if m(2) >= cosd(75), d = m; end
end

function stopRecording(~, ~)
    % Only a confirmed OK stops: closing the dialog or Cancel keeps recording.
    global stopReq;
    if strcmp(questdlg('Stop the recording and save the data?', 'Stop recording', ...
                       'OK', 'Cancel', 'Cancel'), 'OK')
        stopReq = true;
    end
end

function closeRequestHandler(src)
    delete(src);
end

% ============= SAVE SESSION =============
function saveSession(folder, sessionTag, blinkCount, blinkTimestamps, ...
                     blinkIntervals, blinkDurations, minBinCounts, ...
                     minBinAvgInterval, minBinAvgDur, minBinMaxDur, minBinGazeShifts, ...
                     centBlinkCount, centBlinkTimestamps, minBinCentCounts, ...
                     timeLog, diffLog, centYlog, centYampLog, baseYlog, ...
                     ldrTimeLog, ldrValLog, ldrStateLog, ...
                     heartTimeLog, heartFiltLog, heartDerivLog, ...
                     tempTimeLog, tempAmbLog, tempObjLog, fig, lastFrame, suffix, ...
                     heartBeats)
    % heartBeats is a STRUCT, not five more positional arguments. This list is
    % already long enough to mis-order by accident; anything further about the
    % PPG goes in the struct so the signature stops growing.

    baseName = sprintf('session_%s%s', sessionTag, suffix);
    fprintf('Saving: %s\n', baseName);

    % The LDR trace goes into the .mat only (no extra CSV). An automatic
    % threshold switch mid-session is a confound worth being able to find
    % afterwards, and ldrStateLog is where you look for it.
    %
    % V5 adds the PPG and temperature traces. All timestamps come from the same
    % toc(startTime) clock as timeLog, which is the entire point of merging the
    % three rigs — the waveform can be lined up against a blink without having
    % to align three separately-started captures after the fact.
    save(fullfile(folder, [baseName '.mat']), ...
         'blinkCount', 'blinkTimestamps', 'blinkIntervals', 'blinkDurations', ...
         'minBinCounts', 'minBinAvgInterval', 'minBinAvgDur', 'minBinMaxDur', ...
         'minBinGazeShifts', 'centBlinkCount', 'centBlinkTimestamps', 'minBinCentCounts', ...
         'timeLog', 'diffLog', 'centYlog', 'centYampLog', 'baseYlog', ...
         'ldrTimeLog', 'ldrValLog', 'ldrStateLog', ...
         'heartTimeLog', 'heartFiltLog', 'heartDerivLog', ...
         'tempTimeLog', 'tempAmbLog', 'tempObjLog', 'sessionTag');
    % Beat times and the BPM series, appended so the field names are flat in the
    % .mat rather than nested one level down inside a struct — Plot_Captured
    % loads them by name and should not have to know they arrived together.
    beatTimes     = heartBeats.beatTimes;
    bpmTimeLog    = heartBeats.bpmTimeLog;
    bpmValLog     = heartBeats.bpmValLog;
    minBinBpm     = heartBeats.minBinBpm;
    minBinTempObj = heartBeats.minBinTempObj;
    minBinTempAmb = heartBeats.minBinTempAmb;
    sessionStartWall = heartBeats.sessionStartWall;
    ldrCalLog   = heartBeats.ldrCalLog;
    centXlog    = heartBeats.centXlog;
    suppressLog = heartBeats.suppressLog;
    pauseLog    = heartBeats.pauseLog;
    closeDir    = heartBeats.closeDir;
    closedLog   = heartBeats.closedLog;
    stimTimes   = heartBeats.stimTimes;
    stimNames   = heartBeats.stimNames;
    pupilXlog   = heartBeats.pupilXlog;
    pupilYlog   = heartBeats.pupilYlog;
    minBinFixDur   = heartBeats.minBinFixDur;
    minBinGazeMove = heartBeats.minBinGazeMove;
    exposureLog    = heartBeats.exposureLog;
    save(fullfile(folder, [baseName '.mat']), ...
         'beatTimes', 'bpmTimeLog', 'bpmValLog', ...
         'minBinBpm', 'minBinTempObj', 'minBinTempAmb', 'sessionStartWall', 'ldrCalLog', ...
         'centXlog', 'suppressLog', 'pauseLog', 'closeDir', 'closedLog', 'stimTimes', 'stimNames', ...
         'pupilXlog', 'pupilYlog', 'minBinFixDur', 'minBinGazeMove', 'exposureLog', '-append');
    fprintf('  MAT saved\n');
    if ~isempty(stimTimes)
        w = heartBeats.sessionStartWall + seconds(stimTimes(:));
        w.Format = 'yyyy-MM-dd HH:mm:ss.SSS';
        writetable(table(w, stimTimes(:), stimNames(:), 'VariableNames', {'wall_time', 'time_s', 'stimulus'}), ...
            fullfile(folder, [baseName '_stimuli.csv']));
        fprintf('  Stimuli CSV saved (%d marks)\n', numel(stimTimes));
    end

    % V5: PPG and temperature also get CSVs, matching how the motion trace is
    % handled — the .mat is for MATLAB, the CSVs are for everything else.
    if ~isempty(heartTimeLog)
        writetable(table(heartTimeLog(:), heartFiltLog(:), heartDerivLog(:), ...
            'VariableNames', {'Time_s','Filtered','Derivative'}), ...
            fullfile(folder, [baseName '_heart.csv']));
        fprintf('  Heart CSV saved (%d samples)\n', numel(heartTimeLog));
    end
    if ~isempty(heartBeats.bpmTimeLog)
        writetable(table(heartBeats.bpmTimeLog(:), heartBeats.bpmValLog(:), ...
            'VariableNames', {'time_s', 'bpm'}), ...
            fullfile(folder, [baseName '_bpm.csv']));
        fprintf('  BPM CSV saved (%d beats)\n', numel(heartBeats.bpmValLog));
    end

    if ~isempty(tempTimeLog)
        writetable(table(tempTimeLog(:), tempAmbLog(:), tempObjLog(:), ...
            'VariableNames', {'Time_s','Ambient_C','Object_C'}), ...
            fullfile(folder, [baseName '_temperature.csv']));
        fprintf('  Temperature CSV saved (%d samples)\n', numel(tempTimeLog));
    end

    if ~isempty(minBinCounts)
        nMin  = length(minBinCounts);
        pCent = minBinCentCounts(:);  if length(pCent) < nMin, pCent(end+1:nMin) = NaN; end
        pInt  = minBinAvgInterval(:); if length(pInt)  < nMin, pInt(end+1:nMin)  = NaN; end
        pDur  = minBinAvgDur(:);      if length(pDur)  < nMin, pDur(end+1:nMin)  = NaN; end
        pMax  = minBinMaxDur(:);      if length(pMax)  < nMin, pMax(end+1:nMin)  = NaN; end
        padN = @(v) [v(:); nan(nMin - numel(v), 1)];
        writetable(table((1:nMin)', minBinCounts(:), pCent, pInt, pDur, pMax, ...
            padN(minBinGazeShifts), padN(minBinFixDur), padN(minBinGazeMove), ...
            'VariableNames', {'Minute','MotionBlinkCount','CentroidBlinkCount','AvgInterval_s', ...
                              'AvgDuration_s','MaxDuration_s','GazeShifts_per_min', ...
                              'Fixation_s','GazeMovement_px_per_s'}), ...
            fullfile(folder, [baseName '_per_minute.csv']));
        fprintf('  Per-minute CSV saved (%d rows)\n', nMin);
    end

    if blinkCount >= 1
        % Motion blinks only: durations come from the CENTROID detector, a
        % different list. Pairing them by index failed the whole save as soon
        % as there were more centroid closures than motion blinks (16:06 run:
        % 155 vs 129) and every file after this one was never written.
        pI2 = [NaN, blinkIntervals]; pI2(end+1:blinkCount) = NaN; pI2 = pI2(1:blinkCount);
        writetable(table(blinkTimestamps(:), pI2(:), ...
            'VariableNames', {'BlinkTime_s','IntervalFromPrev_s'}), ...
            fullfile(folder, [baseName '_blinks.csv']));
        nC = numel(centBlinkTimestamps); dC = blinkDurations(:); dC(end+1:nC) = NaN;
        writetable(table(centBlinkTimestamps(:), dC(1:nC), ...
            'VariableNames', {'ClosureStart_s','ClosureDuration_s'}), ...
            fullfile(folder, [baseName '_closures.csv']));
        fprintf('  Blinks CSV saved\n');
    end

    if ~isempty(timeLog)
        n   = length(timeLog);
        pC  = centYlog;    if length(pC)  < n, pC(end+1:n)  = NaN; end
        pCa = centYampLog; if length(pCa) < n, pCa(end+1:n) = NaN; end
        pB  = baseYlog;    if length(pB)  < n, pB(end+1:n)  = NaN; end
        pPx = heartBeats.pupilXlog; pPx(end+1:n) = NaN;
        pPy = heartBeats.pupilYlog; pPy(end+1:n) = NaN;
        writetable(table(timeLog(:), diffLog(:), pC(:), pCa(:), pB(:), pPx(1:n).', pPy(1:n).', ...
            'VariableNames', {'Time_s','MotionSignal','CentroidY_px','CentroidYAmp_px','BaselineY_px', ...
                              'PupilX_px','PupilY_px'}), ...
            fullfile(folder, [baseName '_motion.csv']));
        fprintf('  Motion CSV saved\n');
    end

    if ishandle(fig)
        try
            exportgraphics(fig, fullfile(folder, [baseName '_plot.png']), 'Resolution', 150, 'BackgroundColor', [0.08 0.08 0.08]);
            fprintf('  Plot PNG saved\n');
        catch, fprintf('  (plot PNG skipped)\n'); end
    end


    if ~isempty(lastFrame)
        imwrite(uint8(lastFrame), fullfile(folder, [baseName '_lastframe.png']));
        fprintf('  Last frame saved\n');
    end

    fid = fopen(fullfile(folder, [baseName '_summary.txt']), 'w');
    fprintf(fid, 'Session: %s (V5 eye + LDR light state + heart + temperature)\n', sessionTag);
    if ~isempty(heartTimeLog)
        fprintf(fid, 'PPG samples: %d over %.2f min (%.1f Hz effective)\n', ...
                numel(heartTimeLog), heartTimeLog(end)/60, ...
                numel(heartTimeLog) / max(heartTimeLog(end), 0.001));
    else
        fprintf(fid, 'PPG samples: 0 (MAX30102 not found or no packets)\n');
    end
    if ~isempty(tempTimeLog)
        fprintf(fid, 'Temperature: %d samples | object %.2f-%.2f C | ambient %.2f-%.2f C\n', ...
                numel(tempTimeLog), min(tempObjLog), max(tempObjLog), ...
                min(tempAmbLog), max(tempAmbLog));
    else
        fprintf(fid, 'Temperature: 0 samples (MLX90614 not found or no packets)\n');
    end
    if ~isempty(ldrStateLog)
        nSwitch = sum(diff(ldrStateLog) ~= 0);
        fprintf(fid, 'LDR readings: %d | light-state changes: %d | final state: %d (1=DARK 2=INDOOR 3=OUTSIDE)\n', ...
                numel(ldrValLog), nSwitch, ldrStateLog(end));
    else
        fprintf(fid, 'LDR readings: 0 (no LDR packets received this session)\n');
    end
    fprintf(fid, 'Exit: %s\n', suffix);
    fprintf(fid, 'Total blinks (MOTION method):   %d\n', blinkCount);
    fprintf(fid, 'Total blinks (CENTROID method): %d\n', centBlinkCount);
    if blinkCount > 0
        fprintf(fid, 'Centroid/Motion agreement: %.1f%%\n', 100 * centBlinkCount / blinkCount);
    end
    if ~isempty(timeLog)
        durMin = timeLog(end) / 60;
        fprintf(fid, 'Duration: %.2f min\n', durMin);
        fprintf(fid, 'Avg BPM (motion): %.2f\n', blinkCount / max(durMin, 0.001));
    end
    if ~isempty(blinkDurations)
        fprintf(fid, 'Avg blink duration: %.3f s\n', mean(blinkDurations));
        fprintf(fid, 'Max blink duration: %.3f s\n', max(blinkDurations));
    end
    if any(~isnan(minBinGazeShifts))
        fprintf(fid, 'Gaze: %.1f shifts/min, fixation %.2f s, movement %.1f px/s (mean of minutes)\n', ...
            mean(minBinGazeShifts, 'omitnan'), mean(heartBeats.minBinFixDur, 'omitnan'), ...
            mean(heartBeats.minBinGazeMove, 'omitnan'));
    end
    fclose(fid);
    fprintf('  Summary saved\n');
end

function keyPressCallback(src, event)
    global saveTrigger resetBaseline;
    % Typing a stimulus name (spaces, "r") must not trigger the shortcuts.
    co = get(src, 'CurrentObject');
    if ~isempty(co) && isprop(co, 'Style') && strcmp(get(co, 'Style'), 'edit'), return; end
    if strcmp(event.Key, 'space'), saveTrigger   = true; end
    if strcmp(event.Key, 'r'),     resetBaseline = true; end
end

function toggleIRMode(src, ~)
    global irMode switchMode useDarkThresholds;
    % V4: the button is now one of two things that can drive the IR LEDs and
    % the threshold set (an LDR light-state change is the other). A manual
    % click takes effect immediately and holds until the ambient light next
    % crosses a boundary, at which point automatic control resumes.
    irMode = logical(get(src, 'Value'));
    useDarkThresholds = irMode;
    switchMode = true;
    applyIRMode(src, get(gcf, 'UserData'), irMode);
end

function applyIRMode(hBtn, s, on)
    % The ONLY place that touches the IR LEDs or the toggle button's
    % appearance. Both the manual click path and the automatic LDR path call
    % it, so the physical LED state and what the button claims cannot drift
    % apart. Setting 'Value' matters for the automatic path — without it the
    % button would still look un-pressed after an automatic switch, and the
    % next manual click would send the command that is already in effect.
    if on
        try, write(s, uint8(0xF1), 'uint8'); catch, end  % LED ON command to ESP32
        % Black on a brighter amber. The old white-on-dark-orange was low
        % contrast and hard to read at a glance.
        set(hBtn, 'Value', 1, 'String', '⬤  IR LEDs: ON   [DARK]', ...
            'BackgroundColor', [1.0 0.62 0.10], 'ForegroundColor', [0 0 0]);
    else
        try, write(s, uint8(0xF0), 'uint8'); catch, end  % LED OFF command to ESP32
        set(hBtn, 'Value', 0, 'String', '⬤  IR LEDs: OFF  [NORMAL]', ...
            'BackgroundColor', [0.08 0.75 0.08], 'ForegroundColor', [0 0 0]);
    end
end

% ============= V5 SERIAL PROTOCOL HELPERS =============
function tf = isLegalHeader(h)
    % All five packet headers share bytes 1,2,4 and differ only at byte 3:
    % 255 = camera frame, 254 = LDR, 253 = temperature, 252 = heart batch,
    % 251 = subsystem status.
    tf = numel(h) == 4 && h(1) == 255 && h(2) == 0 && h(4) == 0 && ...
         (h(3) >= 251 && h(3) <= 255);
end

function str = decodeStatus(mask, seen)
    % Render the firmware's subsystem bitmask as a compact one-liner.
    % Bits must match Simplified_eye_V6_code.ino:
    %   1 camera   2 LDR   4 heart   8 temp   16 IR LED on
    if ~seen
        str = 'SUBSYSTEMS: (no status packet yet)';
        return;
    end
    names = {'CAM', 'LDR', 'PPG', 'TMP'};
    bits  = [1 2 4 8];
    parts = cell(1, numel(names));
    for k = 1:numel(names)
        if bitand(mask, bits(k))
            parts{k} = sprintf('%s:ok', names{k});
        else
            parts{k} = sprintf('%s:--', names{k});
        end
    end
    if bitand(mask, 16), irStr = 'IR:on'; else, irStr = 'IR:off'; end
    str = [strjoin(parts, ' ') ' ' irStr];
end

function v = decodeInt16LE(b)
    % Two bytes, low first, two's complement. The LDR field is unsigned so V4
    % could just add; temperature and the PPG both go negative and cannot.
    v = double(b(1)) + 256 * double(b(2));
    if v >= 32768, v = v - 65536; end
end

function [filt, deriv] = decodeHeartBatch(raw)
    % raw is 4*N bytes: for each sample, int16 LE filtered then int16 LE
    % derivative. Vectorised — at 100 Hz a per-sample loop here showed up in
    % the frame budget.
    lo = double(raw(1:2:end));
    hi = double(raw(2:2:end));
    v  = lo + 256 * hi;
    v(v >= 32768) = v(v >= 32768) - 65536;   % two's complement
    filt  = v(1:2:end);      % interleaved: filt, deriv, filt, deriv, ...
    deriv = v(2:2:end);
end

function ok = waitBytes(s, n, deadlineSec)
    % Spin until n bytes are buffered. Exists because read() blocks for the
    % full serialport Timeout (10 s by default) on a short read, which would
    % freeze the live figure if a packet arrived truncated. Returns false on
    % timeout so the caller can just skip the packet and resync.
    t0 = tic;
    while s.NumBytesAvailable < n
        if toc(t0) > deadlineSec, ok = false; return; end
        pause(0.001);
    end
    ok = true;
end

function s = ledStr(on)
    % 'ON '/'OFF' — padded so the readout does not jitter as it toggles.
    if on, s = 'ON '; else, s = 'OFF'; end
end

% ============= V4 AUTOMATIC IR LED ARBITRATION =============
function [useDark, havePending, fire] = irAutoResolve(havePending, pendingDark, useDark, sinceSwitch, lockout)
    % Pure decision step for automatic IR LED control, kept separate from the
    % serial loop so it can be tested without hardware.
    %
    %   havePending  a light-state CHANGE has queued a request (a manual
    %                button press never queues one — that is what lets a
    %                manual override hold until the light actually changes)
    %   pendingDark  what that request wants: true = LEDs on + IR thresholds
    %   sinceSwitch  seconds since the last AUTOMATIC change
    %   lockout      seconds to wait before allowing another one
    %
    % Returns the updated threshold selection, whether the request is still
    % outstanding, and whether the caller should drive the LEDs this tick.
    % A request blocked by the lockout stays pending and is retried on later
    % packets rather than being dropped.
    fire = false;
    if ~havePending, return; end
    if pendingDark == useDark
        havePending = false;                 % already where the light wants us
    elseif sinceSwitch >= lockout
        useDark     = pendingDark;
        havePending = false;
        fire        = true;
    end
end

% ============= V4 AMBIENT LIGHT STATE MACHINE =============
function setCalReq()
    global calReq;
    calReq = true;
end

function [T, ok, room, dark] = calibrateLdr(s, hLbl, T)
    % Room for 3 s -> prompt -> covered for 3 s -> new DARK boundary.
    % T is returned unchanged unless the whole sequence succeeds.
    ok = false; dark = NaN;
    pause(0.4);                                % IR LEDs just went off; let the reading settle
    room = measureLdr(s, hLbl, 'Room light');
    if isnan(room)
        warndlg('No LDR readings arrived. Calibration not applied.', 'LDR calibration');
        return;
    end
    a = questdlg(sprintf(['Room light = %d.\n\nNow COVER the photoresistor with your hand ' ...
                          '(keep it covered), then click "Measure dark".'], round(room)), ...
                 'LDR calibration', 'Measure dark', 'Cancel', 'Measure dark');
    if ~strcmp(a, 'Measure dark'), return; end
    dark = measureLdr(s, hLbl, 'Covered (dark)');
    Tn = ldrThresholdsFrom(room, dark, T);
    if isempty(Tn)
        warndlg(sprintf(['Covering the sensor barely changed it (room %d, covered %d).\n' ...
            'Calibration NOT applied -- previous thresholds kept.'], round(room), round(dark)), ...
            'LDR calibration');
        return;
    end
    T = Tn; ok = true;
    msg = sprintf('LDR calibrated: room %d, dark %d -> DARK below %d, back to light above %d.', ...
        round(room), round(dark), round(T.darkLo), round(T.darkHi));
    fprintf('%s\n', msg);
    msgbox(sprintf('%s\n\nYou can uncover the sensor now.', msg), 'LDR calibration');
end

function v = measureLdr(s, hLbl, what)
    % Median LDR reading over 3 s, straight off the port. Median, not mean:
    % the header can occasionally appear inside camera pixel data.
    flush(s);
    buf = zeros(1, 0); t = tic;
    while toc(t) < 3
        if s.NumBytesAvailable > 0
            buf = [buf, read(s, s.NumBytesAvailable, "uint8")]; %#ok<AGROW>
        end
        if ishandle(hLbl)
            set(hLbl, 'String', {sprintf('CALIBRATING: %s', what), ...
                sprintf('%.0f s left', 3 - toc(t))}, 'ForegroundColor', [0.6 0.6 1]);
        end
        drawnow; pause(0.05);
    end
    vals = ldrValuesIn(buf);
    if isempty(vals), v = NaN; else, v = median(vals); end
end

function v = ldrValuesIn(buf)
    % Every LDR reading in a raw byte buffer: [255 0 254 0] + uint16 LE.
    i = strfind(buf, [255 0 254 0]);
    i = i(i + 5 <= numel(buf));
    v = double(buf(i + 4)) + 256 * double(buf(i + 5));
    v = v(v <= 4095);                  % a false match in pixel data can exceed the ADC range
end

function T = ldrThresholdsFrom(room, dark, T)
    % DARK switch point halfway between covered and room light, +/- 10% of
    % the gap as hysteresis. The OUTSIDE boundary is display-only, placed the
    % same distance above the room. [] if covering did not lower the reading
    % by a usable amount (polarity: higher = brighter).
    % ponytail: halfway is a guess; if a dim room still reads DARK, move
    % the 0.5 toward 0.3 (closer to covered).
    span = room - dark;
    if ~(span >= 30), T = []; return; end
    mid = dark + 0.5 * span;
    T.darkLo = mid - 0.1 * span;  T.darkHi = mid + 0.1 * span;
    T.briLo  = room + 0.45 * span; T.briHi = room + 0.55 * span;
end

function st = nextLightState(v, st, T)
    % One hysteretic step. 1=DARK 2=INDOOR 3=OUTSIDE. Identical logic to
    % LDR_LightClassifier.m — keep the two in sync if either is retuned.
    % Only moves one state per reading, so DARK->OUTSIDE takes two LDR
    % packets (~0.2 s at 10 Hz). Fine, and it keeps the branches trivial.
    switch st
        case 1
            if v > T.darkHi, st = 2; end
        case 2
            if     v > T.briHi,  st = 3;
            elseif v < T.darkLo, st = 1;
            end
        case 3
            if v < T.briLo, st = 2; end
    end
end

% =========================================================================
function m = robustScale(v)
    % Half-height for a y-axis that ignores step transients but does NOT clip
    % the pulse. sort + index rather than prctile, so no Statistics Toolbox.
    %
    %   PCT 0.95 — discards the top 5%, where a finger-arrival step, a knock or
    %              a re-grip lives. Higher let too much transient through: the
    %              axis expanded to fit it and every genuine peak shrank away.
    %   HEADROOM — lifts the limit clear of that percentile so real systolic
    %              peaks, which DO exceed it, still draw inside the axis.
    %
    % FLOOR stops an all-zero window (no finger) zooming into its own noise.
    %
    % 25, not the 300 this was copied from. That 300 came from
    % Heart_Waveform_1.m back when ADC_RANGE was 4096; the firmware now runs at
    % 16384 (NOT 32768 - that value is invalid and silently selects 2048 nA),
    % which is 8x less sensitive, so the SAME physical pulse produces roughly
    % 8x fewer counts. A perfectly good +/-40 count PPG sat entirely
    % inside a +/-300 axis and drew as a flat line at zero — indistinguishable
    % from no signal, which is exactly how it was reported.
    %
    % This is the third constant in this project silently invalidated by an
    % ADC_RANGE change (the others: the boot report's dark-channel warning, and
    % the finger presence threshold). Anything compared against a raw count
    % depends on ADC_RANGE. Re-check them together or not at all.
    PCT = 0.95; HEADROOM = 1.3;
    a = sort(abs(v));
    p = a(max(1, ceil(PCT * numel(a))));
    m = max(25, p * HEADROOM);
end

% =========================================================================
function m = meanInWindow(tv, vv, t0, t1)
% Mean of vv over the samples whose timestamps fall in [t0, t1). NaN when the
% window is empty, which is the honest answer for a minute the finger was off
% the sensor — zero would plot as a real reading of zero.
    if isempty(tv) || isempty(vv)
        m = NaN; return;
    end
    n   = min(numel(tv), numel(vv));
    sel = tv(1:n) >= t0 & tv(1:n) < t1;
    if any(sel)
        m = mean(vv(sel), 'omitnan');
    else
        m = NaN;
    end
end

function M = buildMinuteStruct(cnt, cntC, avgD, maxD, gaze, fixD, bpm, tObj, tAmb)
% Pack the per-minute bins for PerMinutePanels, padded to a common length.
% The bins are appended at slightly different points in the loop, so on the
% tick where one has been written and another has not they differ by one.
    n = max([numel(cnt), numel(cntC), numel(avgD), numel(maxD), numel(gaze), numel(fixD), ...
             numel(bpm), numel(tObj), numel(tAmb), 0]);
    pad = @(v) [v(:).', nan(1, n - numel(v))];
    M = struct('minutes', 1:n, 'blinks', pad(cnt), 'blinksCent', pad(cntC), ...
               'avgDur', pad(avgD), 'maxDur', pad(maxD), 'gaze', pad(gaze), 'fixDur', pad(fixD), ...
               'bpm', pad(bpm), 'tempObj', pad(tObj), 'tempAmb', pad(tAmb));
end

function [st, armed, lastBeatT, ibi, beatT, beatIdx] = ...
         ppgBeats(st, armed, lastBeatT, ibi, d, t, sIdx)
% Streaming beat detection on the PPG FIRST DERIVATIVE. Call once per arriving
% batch, carrying the state back in (st starts as [] or 0).
%
% Why the derivative: the systolic upstroke is the sharpest, most repeatable
% feature of a PPG; the rounded peak and the dicrotic notch next to it make a
% peak finder double-count.
%
% ADAPTIVE (2026-10-28). The old detector had a fixed 15-count floor and a peak
% envelope that took ~10 s to forget a motion spike. On the 16:22 recording the
% pulse was plainly there but its upslope peaked at ~12 counts, so it found 5
% BPM values in 2.6 min. Now, from the last 4 s of a 5-sample-smoothed
% derivative (recomputed every 25 samples):
%   - POLARITY: the side (up / down) with the larger 97th-percentile swing is
%     the beat edge -- on the 16:22 placement the sharp edge was the dip.
%   - THRESHOLD = max(0.5 x that swing, NOISE_K x sample-to-sample jitter, 2 counts).
% A motion spike leaves the 4 s window by itself. Checked on four real
% sessions: 16:06 651 beats found vs ~644 expected (67 BPM); 16:22 178 vs ~183.
    WIN = 400; UPD = 25; FRAC = 0.5; NOISE_K = 4; ABS_MIN = 2;
    REARM_FRAC  = 0.30;   % must fall back under this*threshold before re-firing
    REFRACTORY  = 0.30;   % s, hard floor between beats -> 200 BPM ceiling
    IBI_MIN     = 0.27;   % s -> 222 BPM
    IBI_MAX     = 2.00;   % s ->  30 BPM
    IBI_KEEP    = 8;      % intervals in the median window
    if ~isstruct(st)
        st = struct('sm', zeros(1, 4), 'buf', nan(1, WIN), 'bi', 0, 'n', 0, 'pol', 1, 'thr', Inf);
    end

    beatT = []; beatIdx = [];
    for k = 1:numel(d)
        y = (sum(st.sm) + d(k)) / 5; st.sm = [st.sm(2:end) d(k)];
        st.bi = mod(st.bi, WIN) + 1; st.buf(st.bi) = y; st.n = st.n + 1;
        if mod(st.n, UPD) == 0 && st.n >= 100
            q = sort(st.buf(~isnan(st.buf))); m = numel(q);
            hi = q(ceil(0.97 * m)); lo = -q(max(1, floor(0.03 * m)));
            if lo > 1.3 * hi, st.pol = -1; elseif hi > 1.3 * lo, st.pol = 1; end
            b = st.buf([st.bi+1:end, 1:st.bi]); b = b(~isnan(b));   % time order
            noise = 1.4826 * median(abs(diff(b))) / sqrt(2);          % sample-to-sample jitter
            st.thr = max([FRAC * max(hi, lo), NOISE_K * noise, ABS_MIN]);
        end
        s = st.pol * y;

        if armed && s > st.thr && (t(k) - lastBeatT) > REFRACTORY
            gap = t(k) - lastBeatT;
            if isfinite(gap) && gap >= IBI_MIN && gap <= IBI_MAX
                ibi = [ibi, gap]; %#ok<AGROW>
                if numel(ibi) > IBI_KEEP, ibi = ibi(end-IBI_KEEP+1:end); end
            elseif isfinite(gap) && gap > IBI_MAX
                ibi = [];   % pulse lost and back: do not average across the gap
            end
            beatT(end+1)   = t(k);      %#ok<AGROW>
            beatIdx(end+1) = sIdx(k);   %#ok<AGROW>
            lastBeatT = t(k);
            armed = false;              % latch until the signal falls back
        elseif ~armed && s < REARM_FRAC * st.thr
            armed = true;
        end
    end
end

function ok = steadyPulse(ibi)
    % BPM is only shown / logged when the recent beats agree: the middle of
    % the last 5 intervals within 20% of their median. Noise with the sensor
    % off the skin makes irregular "beats" that fail this.
    ok = false;
    if numel(ibi) < 5, return; end
    q = sort(ibi(end-4:end));
    ok = (q(4) - q(2)) / q(3) < 0.2;
end

function lim = fitAxis(cur, want)
    % Hysteresis band. Recomputing limits on every redraw made the trace
    % breathe continuously so small features were never on a steady scale.
    % Leave the axis alone while it is within 25% of what the data wants.
    if want > cur(2) || want < 0.75 * cur(2)
        lim = [-1 1] * want;
    else
        lim = cur;
    end
end

% =========================================================================
function n = packetLength(t, buf)
    % TOTAL bytes of the packet whose type byte is t, header included.
    % -1 means the length cannot be known yet (a heart batch whose count byte
    % has not arrived), which the caller treats as "wait for more".
    switch t
        case 255, n = 4 + 6400;          % camera frame
        case 254, n = 4 + 2;             % LDR uint16
        case 253, n = 4 + 4;             % temperature, 2x int16
        case 251, n = 4 + 1;             % status bitmask
        case 252                          % heart: 1 count byte, then N x 4
            if numel(buf) < 5, n = -1; else, n = 4 + 1 + 4 * double(buf(5)); end
        otherwise, n = -1;
    end
end

% =========================================================================
function idx = findHeaderFrom(buf, startAt)
    % First index >= startAt where a legal 4-byte header begins, or [].
    % strfind over the whole array rather than a byte-at-a-time scan: the old
    % resync read ONE BYTE PER serialport call, which on a driver with ~16 ms
    % transaction latency could take seconds to recover from a single glitch.
    if startAt < 1, startAt = 1; end
    seg = buf(startAt:end);
    idx = [];
    for hb = 255:-1:251
        h = strfind(seg, [255 0 hb 0]);
        if ~isempty(h)
            c = h(1) + startAt - 1;
            if isempty(idx) || c < idx, idx = c; end
        end
    end
end

% ============= V5 SELF-CHECK =============
% Reached via  NOT_Simplified_eye_V7_win_to_mac('test')  — a local function is not callable
% from the command window on its own. Checks only the pure logic: the protocol
% decoders, the light-state machine and the IR arbitration. The tracking
% algorithm is unchanged from V3/V4 and is not re-tested here.
function selfTest()
    T.darkLo = 457; T.darkHi = 537; T.briLo = 800; T.briHi = 830;

    % Header discrimination — the thing that keeps the four streams apart.
    assert( isLegalHeader(uint8([255 0 255 0])), 'camera header must be legal');
    assert( isLegalHeader(uint8([255 0 254 0])), 'LDR header must be legal');
    assert( isLegalHeader(uint8([255 0 253 0])), 'temperature header must be legal');
    assert( isLegalHeader(uint8([255 0 252 0])), 'heart header must be legal');
    assert( isLegalHeader(uint8([255 0 251 0])), 'status header must be legal');
    assert(~isLegalHeader(uint8([255 0 250 0])), 'unknown type must be rejected');
    assert(~isLegalHeader(uint8([255 0 255 1])), 'bad trailing byte must be rejected');
    assert(~isLegalHeader(uint8([0 0 255 0])),   'bad leading byte must be rejected');

    % --- V5: subsystem status bitmask ---
    % Bits must match Simplified_eye_V6_code.ino: 1 CAM, 2 LDR, 4 PPG, 8 TMP,
    % 16 IR LED. Getting these out of step would silently mislabel the display.
    assert(contains(decodeStatus(0, false), 'no status packet'), ...
           'unseen status must say so rather than claim everything is absent');
    all4 = decodeStatus(1 + 2 + 4 + 8, true);
    assert(contains(all4, 'CAM:ok') && contains(all4, 'LDR:ok') && ...
           contains(all4, 'PPG:ok') && contains(all4, 'TMP:ok'), 'all-present mask');
    assert(contains(all4, 'IR:off'), 'IR bit clear must read off');
    none = decodeStatus(0, true);
    assert(contains(none, 'CAM:--') && contains(none, 'LDR:--') && ...
           contains(none, 'PPG:--') && contains(none, 'TMP:--'), 'all-absent mask');
    % Each bit independently — an ordering slip would only show up here.
    assert(contains(decodeStatus(1, true),  'CAM:ok'), 'bit0 = camera');
    assert(contains(decodeStatus(2, true),  'LDR:ok'), 'bit1 = LDR');
    assert(contains(decodeStatus(4, true),  'PPG:ok'), 'bit2 = heart');
    assert(contains(decodeStatus(8, true),  'TMP:ok'), 'bit3 = temperature');
    assert(contains(decodeStatus(16, true), 'IR:on'),  'bit4 = IR LED');
    % The realistic no-camera case: everything but the camera present.
    noCam = decodeStatus(2 + 4 + 8, true);
    assert(contains(noCam, 'CAM:--') && contains(noCam, 'LDR:ok') && ...
           contains(noCam, 'PPG:ok'), 'camera absent, sensors present');

    % Little-endian decode must match what the .ino writes (low byte first).
    b = uint8([mod(827,256), floor(827/256)]);
    assert(double(b(1)) + 256*double(b(2)) == 827, 'LE decode of 827');
    b = uint8([mod(1023,256), floor(1023/256)]);
    assert(double(b(1)) + 256*double(b(2)) == 1023, 'LE decode of full scale');

    % --- V5: signed int16 decode (temperature and PPG both go negative) ---
    assert(decodeInt16LE(uint8([0 0]))       ==  0,     'int16 zero');
    assert(decodeInt16LE(uint8([1 0]))       ==  1,     'int16 one');
    assert(decodeInt16LE(uint8([255 127]))   ==  32767, 'int16 max');
    assert(decodeInt16LE(uint8([0 128]))     == -32768, 'int16 min');
    assert(decodeInt16LE(uint8([255 255]))   == -1,     'int16 minus one');
    % 23.44 C is sent as 2344 hundredths; -5.25 C as -525.
    assert(decodeInt16LE(uint8([mod(2344,256) floor(2344/256)])) / 100 == 23.44, ...
           'positive temperature round-trip');
    neg = typecast(int16(-525), 'uint8');
    assert(decodeInt16LE(neg) / 100 == -5.25, 'negative temperature round-trip');

    % --- V5: heart batch decode ---
    % Build the exact byte stream the firmware writes for three samples, then
    % check both channels come back in the right order with the right signs.
    want  = int16([ 412 -1730;  -88   250;  0   0]);
    bytes = uint8([]);
    for k = 1:size(want, 1)
        bytes = [bytes, typecast(want(k,1), 'uint8'), typecast(want(k,2), 'uint8')]; %#ok<AGROW>
    end
    [f, d] = decodeHeartBatch(bytes);
    assert(isequal(f, double(want(:,1))'), 'heart filtered channel mis-decoded');
    assert(isequal(d, double(want(:,2))'), 'heart derivative channel mis-decoded');
    assert(numel(f) == 3 && numel(d) == 3, 'heart batch length wrong');

    % A single-sample batch is the common case at low frame rates.
    [f1, d1] = decodeHeartBatch([typecast(int16(-1), 'uint8'), typecast(int16(32767), 'uint8')]);
    assert(f1 == -1 && d1 == 32767, 'single-sample batch');

    % The scrolling window shift must keep the newest samples at the right end
    % and drop exactly as many from the left.
    win = 1:10;
    nNew = 3;
    win  = [win(nNew+1:end), [91 92 93]];
    assert(numel(win) == 10, 'window must keep its length');
    assert(isequal(win(end-2:end), [91 92 93]), 'newest samples go on the right');
    assert(win(1) == 4, 'oldest samples must be dropped');

    % Batch timestamps: evenly spaced back from the arrival time at 100 Hz,
    % ending exactly at it. An off-by-one here would skew the PPG against the
    % blink times, which is the whole reason for merging the rigs.
    tArr = 12.34; n = 4;
    ts = tArr - (n-1:-1:0) / 100;
    assert(numel(ts) == n, 'one timestamp per sample');
    assert(abs(ts(end) - tArr) < 1e-12, 'last sample lands at arrival time');
    assert(abs(ts(1) - (tArr - 0.03)) < 1e-12, 'first sample back-dated by (n-1)/100');
    assert(all(diff(ts) > 0), 'timestamps must increase');

    % Hysteresis holds inside each band...
    assert(nextLightState(497, 1, T) == 1, 'DARK held below darkHi');
    assert(nextLightState(497, 2, T) == 2, 'INDOOR held above darkLo');
    assert(nextLightState(815, 2, T) == 2, 'INDOOR held below briHi');
    assert(nextLightState(815, 3, T) == 3, 'OUTSIDE held above briLo');
    % ...and releases past the far edge.
    assert(nextLightState(538, 1, T) == 2, 'DARK -> INDOOR');
    assert(nextLightState(456, 2, T) == 1, 'INDOOR -> DARK');
    assert(nextLightState(831, 2, T) == 3, 'INDOOR -> OUTSIDE');
    assert(nextLightState(799, 3, T) == 2, 'OUTSIDE -> INDOOR');

    % Dithering on a boundary must not flip the state even once — this is
    % what stops the IR threshold set from thrashing at dusk.
    st = 2;
    for v = repmat([829 801 828 800.5 815], 1, 4)
        st = nextLightState(v, st, T);
        assert(st == 2, 'dither across 815 must not flip state');
    end
    st = 2;
    for v = repmat([536 458 535 457.5 497], 1, 4)
        st = nextLightState(v, st, T);
        assert(st == 2, 'dither across 497 must not flip state');
    end

    % Observed calibration extremes (n=141, 10 Aug 2026) land in the right
    % bucket. The DARK boundary has 165 counts of margin on each side.
    assert(nextLightState(292, 2, T) == 1, 'dark max (292) reads DARK');
    assert(nextLightState(64,  2, T) == 1, 'dark min (64) reads DARK');
    assert(nextLightState(703, 1, T) == 2, 'indoor min (703) reads INDOOR');
    assert(nextLightState(783, 1, T) == 2, 'indoor max (783) stays INDOOR');
    assert(nextLightState(851, 2, T) == 3, 'bright cluster start (851) reads OUTSIDE');
    assert(nextLightState(1023,2, T) == 3, 'full scale reads OUTSIDE');
    % The three dim-outdoor outliers deliberately read INDOOR — harmless,
    % since INDOOR and OUTSIDE both mean "IR LEDs off".
    assert(nextLightState(792, 2, T) == 2, 'dim-outdoor 792 accepted as INDOOR');

    % First-packet seeding (no hysteresis) must agree with the midpoints and
    % must bucket each class median correctly.
    seed = @(v) 1 + (v >= 497) + (v >= 815);
    assert(seed(195) == 1 && seed(766) == 2 && seed(949) == 3, 'seed from class medians');
    assert(seed(496) == 1 && seed(497) == 2, 'seed boundary at 497');
    assert(seed(814) == 2 && seed(815) == 3, 'seed boundary at 815');

    % --- automatic IR LED arbitration ---
    LOCK = 3.0;

    % Nothing queued -> never touches the LEDs.
    [ud, hp, fire] = irAutoResolve(false, true, false, 99, LOCK);
    assert(~fire && ~hp && ud == false, 'no request must do nothing');

    % Queued but already in the requested state -> clears without switching.
    [~, hp, fire] = irAutoResolve(true, true, true, 99, LOCK);
    assert(~fire && ~hp, 'redundant request must clear without firing');

    % Queued, mismatched, lockout still running -> stays queued, no fire.
    [ud, hp, fire] = irAutoResolve(true, true, false, 1.0, LOCK);
    assert(~fire && hp && ud == false, 'lockout must suppress but keep the request');

    % ...and the SAME request must land once the lockout expires. This is the
    % retry that a plain transition-gated version would have dropped.
    [ud, hp, fire] = irAutoResolve(hp, true, ud, 3.1, LOCK);
    assert(fire && ~hp && ud == true, 'suppressed request must retry after lockout');

    % Going dark -> bright works the same way.
    [ud, hp, fire] = irAutoResolve(true, false, true, 5.0, LOCK);
    assert(fire && ~hp && ud == false, 'DARK -> BRIGHT must switch LEDs off');

    % Boundary: exactly at the lockout counts as expired.
    [~, ~, fire] = irAutoResolve(true, true, false, LOCK, LOCK);
    assert(fire, 'lockout boundary is inclusive');

    % A manual override queues nothing, so it must survive repeated packets.
    ud = true;                       % user forced IR on while INDOOR
    for k = 1:20
        [ud, hp, fire] = irAutoResolve(false, false, ud, 99, LOCK);
        assert(~fire && ~hp && ud == true, 'manual override must not be auto-reverted');
    end

    % --- V5: heart-panel axis scaling (ported from Heart_Waveform_1.m) ---
    % A flat window must not zoom into its own noise.
    assert(robustScale(zeros(1, 100)) == 25, 'flat window must hit the floor');
    % A finger-arrival step in the top 5% must NOT set the scale — that is the
    % whole reason for the percentile.
    v = [ones(1, 99) * 100, 200000];
    assert(robustScale(v) < 1000, 'a single huge outlier must not drive the axis');
    % A small but REAL signal must scale to fit, not be swallowed by the floor.
    % This is the case the old 300 floor got wrong.
    assert(robustScale([ones(1, 95) * 30, ones(1, 5) * 40]) < 100, ...
           'a +/-40 count PPG must not be flattened by the floor');
    % Real peaks still fit: the 95th percentile times 1.3 headroom.
    v2 = [ones(1, 95) * 100, ones(1, 5) * 150];
    assert(robustScale(v2) >= 130, 'genuine peaks must stay inside the axis');

    % fitAxis leaves the axis alone inside its hysteresis band, and moves it
    % outside. Without the band the trace breathes on every redraw.
    assert(isequal(fitAxis([-1000 1000], 900), [-1000 1000]), 'inside band: hold');
    assert(isequal(fitAxis([-1000 1000], 1200), [-1200 1200]), 'above: expand');
    assert(isequal(fitAxis([-1000 1000], 500), [-500 500]), 'well below: shrink');
    assert(isequal(fitAxis([-1000 1000], 800), [-1000 1000]), '0.8 is inside 0.75');

    % --- BEAT DETECTION (V6) ---
    % A clean synthetic pulse at a known rate must come back at that rate.
    fs = 100; tt = 0:1/fs:20-1/fs; fBeat = 72/60;
    sig = 100 * sin(2*pi*fBeat*tt);
    d   = [0, diff(sig)] * fs / 10;          % into a realistic count range
    [~, ~, ~, ibiT, btT, biT] = ppgBeats(0, true, -inf, [], d, tt, 1:numel(tt));
    assert(abs(60/median(ibiT) - 72) < 2, 'clean 72 BPM must read back as 72');
    assert(numel(btT) == numel(biT), 'beat times and indices must stay paired');

    % The dicrotic notch is the failure this detector exists to avoid: a second
    % smaller bump right after systole. It must NOT be counted as its own beat.
    sig2 = 100*sin(2*pi*fBeat*tt) + 35*sin(2*pi*2*fBeat*tt + 0.9);
    d2   = [0, diff(sig2)] * fs / 10;
    [~, ~, ~, ibi2, bt2] = ppgBeats(0, true, -inf, [], d2, tt, 1:numel(tt));
    assert(abs(60/median(ibi2) - 72) < 4, 'dicrotic notch must not double-count');
    assert(numel(bt2) < 20*fBeat*1.5, 'notch produced extra beats');

    % Flat input (no finger) must produce no beats at all, rather than locking
    % onto noise and reporting a confident number.
    [~, ~, ~, ~, bt3] = ppgBeats(0, true, -inf, [], zeros(1,1000), ...
                                 (0:999)/fs, 1:1000);
    assert(isempty(bt3), 'no finger must yield no beats');

    % A gap longer than IBI_MAX must reset the history instead of averaging
    % across it and reporting a rate nobody had.
    tGap = [tt(1:500), tt(501:end) + 5];
    [~, ~, ~, ibi4] = ppgBeats(0, true, -inf, [], d, tGap, 1:numel(tGap));
    assert(all(ibi4 < 2.0), 'the 5 s dropout must not enter the interval median');

    % --- PER-MINUTE BINNING (V6) ---
    % The window is half-open [t0, t1): a sample exactly on the boundary belongs
    % to the NEXT minute, or it would be counted in both.
    tv = [0 30 59.9 60 90 119.9 120];
    vv = [1  2    3   4  5     6   7];
    assert(meanInWindow(tv, vv,   0,  60) == 2,   'minute 1 mean');
    assert(meanInWindow(tv, vv,  60, 120) == 5,   'minute 2 mean');
    assert(isnan(meanInWindow(tv, vv, 180, 240)), 'empty minute must be NaN');
    assert(isnan(meanInWindow([], [], 0, 60)), 'no data must be NaN, not an error');
    % Logs are appended at slightly different points in the loop, so one can be
    % a sample longer than the other on any given tick.
    assert(meanInWindow(tv, vv(1:5), 0, 60) == 2, 'mismatched lengths must not throw');

    % buildMinuteStruct pads short bins so every panel shares one x-axis.
    Mt = buildMinuteStruct([8 9 7], [8 9], [0.2 0.2 0.2], [0.3 0.3 0.3], ...
                           [2 2 2], [1 1 1], [70 72], [34 34 34], [24 24 24]);
    assert(numel(Mt.blinks) == 3 && numel(Mt.blinksCent) == 3, 'padded to longest');
    assert(isnan(Mt.blinksCent(3)) && isnan(Mt.bpm(3)), 'pad value must be NaN');
    assert(isequal(Mt.minutes, 1:3), 'minute index must match the bin count');

    % --- V7 exposure loop: dark -> exposure up (<=10%), then gain; bright -> down ---
    [a1, g1] = nextExposure(300, 2, 0.5, 8);  assert(a1 == 330 && g1 == 2, 'dark: +10% exposure');
    [a2, g2] = nextExposure(400, 2, 0.5, 8);  assert(a2 == 400 && g2 == 3, 'exposure maxed (400): gain +1');
    [a6, ~]  = nextExposure(380, 2, 0.5, 8);  assert(a6 == 400, 'exposure capped at 400');
    [a3, g3] = nextExposure(300, 2, 1.0, 8);  assert(a3 == 300 && g3 == 2, 'on target: no change');
    [a4, g4] = nextExposure(300, 4, 1.5, 8);  assert(a4 == 300 && g4 == 3, 'bright: gain down first');
    [a5, ~]  = nextExposure(300, 2, 1.5, 8);  assert(a5 == 270, 'bright at base gain: -10% exposure');

    % --- V7 blob tracker: a round pupil beats a lash line; closed = faded ---
    [C2, R2] = meshgrid(1:80);
    Eb = 180 * ones(80); Eb(hypot(C2 - 45, R2 - 35) <= 5) = 40; Eb(10:12, 10:70) = 40;
    [sO, pO] = blobScore(Eb, []);
    assert(norm(pO - [45 35]) <= 1.5, 'blob tracker must pick the round pupil, not the lash line');
    El2 = 180 * ones(80); El2(48:50, 20:70) = 40;
    assert(blobScore(El2, []) < 0.7 * sO, 'a closed lid (lash line only) must score well below an open pupil');
    win = hypot(C2 - 45, R2 - 35) <= 22; Eb2 = Eb; Eb2(hypot(C2 - 10, R2 - 70) <= 8) = 0;
    [~, pW] = blobScore(Eb2, win);
    assert(norm(pW - [45 35]) <= 1.5, 'the eye window must exclude a darker blob outside it');

    % --- Gaze: shifts and fixations ---
    tg = 0:0.125:30; xg = 40 + 6 * (tg >= 5) + 6 * (tg >= 10) + 6 * (tg >= 15); yg = 40 + 0 * tg;
    xg(tg > 12 & tg < 12.5) = NaN;                      % a blink inside the 2nd fixation
    G = GazeMetrics(tg, xg, yg);
    assert(isequal(G.shiftT, [5 10 15]), 'three gaze shifts at 5, 10, 15 s');
    assert(numel(G.fixDur) == 1 && abs(G.fixDur - 4.875) < 1e-9, 'blink-interrupted fixation dropped');
    assert(abs(G.rate - 3 / (G.trackedSec / 60)) < 1e-9 && G.trackedSec < 30, 'rate per tracked minute');

    % --- V5: bulk-read framing (the fix for the serialport transaction cap) ---
    assert(packetLength(255, []) == 6404, 'frame length');
    assert(packetLength(254, []) == 6,    'LDR length');
    assert(packetLength(253, []) == 8,    'temperature length');
    assert(packetLength(251, []) == 5,    'status length');
    assert(packetLength(252, [255 0 252 0 3]) == 4+1+12, 'heart length from count byte');
    assert(packetLength(252, [255 0 252 0]) == -1, 'heart length unknown until count arrives');
    assert(packetLength(250, []) == -1, 'unknown type must report unknown');

    % Header search: alignment, offset, and absence.
    assert(findHeaderFrom([255 0 254 0 9 9], 1) == 1, 'header at front');
    assert(findHeaderFrom([7 7 255 0 251 0 1], 1) == 3, 'header after garbage');
    assert(isempty(findHeaderFrom([1 2 3 4 5], 1)), 'no header returns empty');
    % The EARLIEST header wins even when a later type sorts first.
    assert(findHeaderFrom([255 0 252 0 0 255 0 255 0], 1) == 1, 'earliest header wins');

    % A packet split across two reads must not be consumed early: the framing
    % waits, which is the case a read-per-field parser handled with a blocking
    % wait and this one handles by simply not slicing yet.
    part = [255 0 253 0 1 2];                 % temperature, 2 of 4 payload bytes
    assert(numel(part) < packetLength(253, part), 'split packet must be incomplete');

    % --- closing direction ---
    assert(isequal(closeDirFrom(zeros(0, 2)), [0 1]), 'default straight down');
    diag45 = repmat([sind(40) cosd(40)], 6, 1);
    assert(abs(atan2d(closeDirFrom(diag45) * [1; 0], closeDirFrom(diag45) * [0; 1]) - 40) < 1e-6, 'learns a 40 deg closing direction');
    assert(isequal(closeDirFrom(repmat([1 0], 6, 1)), [0 1]), 'sideways (gaze) is never learnt');
    assert(isequal(closeDirFrom([repmat([1 0], 3, 1); repmat([-1 0], 3, 1)]), [0 1]), 'disagreeing blinks keep the default');

    % --- LDR calibration ---
    Tc = ldrThresholdsFrom(300, 60, T);                 % dim room 300, covered 60
    assert(Tc.darkLo == 156 && Tc.darkHi == 204, 'DARK boundary halfway, +/-10%');
    assert(nextLightState(250, 2, Tc) == 2, 'the dim room stays INDOOR once calibrated');
    assert(nextLightState(250, 2, T) == 1, '...where the default thresholds called it DARK');
    assert(nextLightState(80, 2, Tc) == 1 && nextLightState(220, 1, Tc) == 2, 'cover / uncover');
    assert(isempty(ldrThresholdsFrom(300, 290, T)), 'no usable change -> not applied');
    assert(isempty(ldrThresholdsFrom(60, 300, T)), 'covering must LOWER the reading');
    assert(isempty(ldrThresholdsFrom(NaN, 60, T)), 'no readings -> not applied');
    buf = [9 255 0 254 0 44 1  7 7  255 0 255 0 1 2  255 0 254 0 200 0  255 0 254 0 5];
    assert(isequal(ldrValuesIn(buf), [300 200]), 'LDR values from a raw stream, partial last dropped');

    fprintf(['NOT_Simplified_eye_V7: protocol (5 packet types) + status bitmask + ' ...
             'int16/heart decode + light-state + IR-arbitration + ' ...
             'heart-axis scaling + BPM beat-detection + per-minute binning + LDR calibration + closing-direction + gaze + exposure checks passed.\n']);
end
