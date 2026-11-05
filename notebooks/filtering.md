PPG processing, filtering and beat detection
- ★ Charlton PH et al., "Wearable photoplethysmography for cardiovascular monitoring," Proc. IEEE 110(3), 2022. The best single overview of the whole pipeline.
- ★ Liang Y, Elgendi M, Chen Z, Ward R, "An optimal filter for short photoplethysmogram signals," Sci. Data 5:180076, 2018. Compares filter types, orders and bands against a signal-quality score, which is close to what we're automating.
- Charlton PH et al., "Detecting beats in the photoplethysmogram: benchmarking open-source algorithms," Physiol. Meas. 43(8), 2022. Covers how to score beat detectors against a reference, which is our finger-agreement idea.
- Charlton PH et al., "The 2023 wearable photoplethysmography roadmap," Physiol. Meas. 44(11), 2023.
- Elgendi M, "On the analysis of fingertip photoplethysmogram signals," Curr. Cardiol. Rev. 8(1), 2012. Covers waveform landmarks and the second-derivative (APG) waves.

Signal-quality indices (the Layer 1 quality gates)
- ★ Elgendi M, "Optimal signal quality index for photoplethysmogram signals," Bioengineering 3(4):21, 2016. Source of the skewness check we use.
- Orphanidou C et al., "Signal-quality indices for the ECG and PPG…," IEEE JBHI 19(3), 2015. Template-matching quality score.
- Li Q, Clifford GD, "Dynamic time warping and machine learning for signal quality assessment of pulsatile signals," Physiol. Meas. 33(9), 2012.

Waveform features related to BP
- Takazawa K et al., Hypertension 32(2), 1998. Second-derivative (APG) wave ratios and the ageing index.
- Teng XF, Zhang YT, IEEE EMBC 2003. Pulse widths vs. BP.
- Mukkamala R et al., "Toward ubiquitous blood pressure monitoring via pulse transit time," IEEE TBME 62(8), 2015.
- ★ González S, Hsieh WT, Chen TPC, "A benchmark for ML-based non-invasive BP estimation using PPG," Sci. Data 10:149, 2023. A sobering read on how optimistic most published results are.

Tuning methodology and the overfitting trap
- ★ Cawley GC, Talbot NLC, "On over-fitting in model selection and subsequent selection bias in performance evaluation," JMLR 11, 2010. The reason we don't tune against BP.
- Akiba T et al., "Optuna," KDD 2019, and Bergstra J, Bengio Y, "Random search for hyper-parameter optimization," JMLR 13, 2012.
- Deb K et al., "NSGA-II," IEEE TEVC 6(2), 2002. Multi-objective search that returns a set of best trade-offs.

Validation, for the later BP layer
- Bland JM, Altman DG, Lancet 1986. Bland-Altman agreement plots.
- Stergiou GS et al., AAMI/ESH/ISO validation standard, Hypertension 71(3), 2018.

Open-source toolboxes to compare against: pyPPG (Goda, Charlton, Behar, Physiol. Meas. 2024), NeuroKit2, HeartPy.
