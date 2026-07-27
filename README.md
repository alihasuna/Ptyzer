# Ptyzer

This project contains utilities for working with four-dimensional scanning transmission electron microscopy (4D-STEM) datasets, with an emphasis on ptychography and applications that use Azorus 4D STEM software produced by Hitachi High-Tech Canada.

The initial commit provides a core set of functions for converting Azorus generated `*.hp` files into py4DSTEM's emdfile `.h5` format. Both Azorus `.hp` and py4DSTEM `.h5` files store data in HDF5 formats, but differ in their internal structure. The core conversion function `azohp_to_py4d`, provided here in `io/azohp_to_py4d.py`, reshapes the microscopy data, as appropriate, and embeds all the metadata from the Azorus `.hp` file into the py4DSTEM (emdfile format) `.h5` file.

The embedded metadata typically includes the microscope lens, coil, and stage coordinates used during the acquisitions, along with timing and scan parameters.

The functions also provide features for performing basic checks, preprocessing and analysis of data that are useful to perform prior to forming a ptychographic reconstruction. The results of the analysis are embedded into the output `.h5` file.

Utility functions to link into other ptychographic solvers, such as modified versions of
PtychoShelves, such as provided at [PtychoRunner](https://github.com/ArthurBlackburn/PtychoRunner), or into advanced distortion correction routines, such as[emicroml](https://github.com/mrfitzpa/emicroml), will be provided in due course.
