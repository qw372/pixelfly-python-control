import re
import sys
import h5py
import time
import logging
import traceback
import configparser
import numpy as np
from scipy import optimize
import matplotlib.pyplot as plt
from matplotlib import cm
import PyQt5
import pyqtgraph as pg
import PyQt5.QtGui as QtGui
import PyQt5.QtWidgets as qt
import os
import pco
import qdarkstyle # see https://github.com/ColinDuquesnoy/QDarkStyleSheet


# steal colormap data from matplotlib
def steal_colormap(colorname="viridis", lut=6):
    color = cm.get_cmap(colorname, lut)
    colordata = color(range(lut)) # (r, g, b, a=opacity)
    colordata_reform = []
    for i in range(lut):
        l = [i/lut, tuple(colordata[i]*255)]
        colordata_reform.append(tuple(l))

    return colordata_reform

def fake_data(xmax, ymax):
    x_range=20
    y_range=30
    x_center=12
    y_center=17
    x_err=3
    y_err=2
    amp=100
    noise_amp = 10
    x, y = np.meshgrid(np.arange(x_range), np.arange(y_range))
    dst = np.sqrt((x-x_center)**2/(2*x_err**2)+(y-y_center)**2/2/(2*y_err**2)).T
    gauss = np.exp(-dst)*amp + np.random.random_sample(size=(x_range, y_range))*noise_amp
    gauss = np.repeat(gauss, round(xmax/x_range), axis=0)
    gauss = np.repeat(gauss, round(ymax/y_range), axis=1)

    return gauss

def gaussian(amp, x_mean, y_mean, x_width, y_width, offset):
    x_width = float(x_width)
    y_width = float(y_width)

    return lambda x, y: amp*np.exp(-0.5*((x-x_mean)/x_width)**2-0.5*((y-y_mean)/y_width)**2) + offset

def gaussianfit(data):
    # codes adapted from https://scipy-cookbook.readthedocs.io/items/FittingData.html
    # calculate moments for initial guess
    total = np.sum(data)
    X, Y = np.indices(data.shape)
    x_mean = np.sum(X*data)/total
    y_mean = np.sum(Y*data)/total
    col = data[:, int(y_mean)]
    x_width = np.sqrt(np.abs((np.arange(col.size)-x_mean)**2*col).sum()/col.sum())
    row = data[int(x_mean), :]
    y_width = np.sqrt(np.abs((np.arange(row.size)-y_mean)**2*row).sum()/row.sum())
    offset = (data[0, :].sum()+data[-1, :].sum()+data[:, 0].sum()+data[:, -1].sum())/np.sum(data.shape)/2
    amp = data.max() - offset

    errorfunction = lambda p: np.ravel(gaussian(*p)(*np.indices(data.shape))-data)
    p, success = optimize.leastsq(errorfunction, (amp, x_mean, y_mean, x_width, y_width, offset))

    p_dict = {}
    p_dict["x_mean"] = p[1]
    p_dict["y_mean"] = p[2]
    p_dict["x_width"] = p[3]
    p_dict["y_width"] = p[4]
    p_dict["amp"] = p[0]
    p_dict["offset"] = p[5]

    return p_dict

class Scrollarea(qt.QGroupBox):
    def __init__(self, parent, label="", type="grid"):
        super().__init__()
        self.parent = parent
        self.setTitle(label)
        outer_layout = qt.QGridLayout()
        outer_layout.setContentsMargins(0,0,0,0)
        self.setLayout(outer_layout)

        scroll = qt.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameStyle(0x10)
        outer_layout.addWidget(scroll)

        box = qt.QWidget()
        scroll.setWidget(box)
        if type == "form":
            self.frame = qt.QFormLayout()
        elif type == "grid":
            self.frame = qt.QGridLayout()
        elif type == "vbox":
            self.frame = qt.QVBoxLayout()
        elif type == "hbox":
            self.frame = qt.QHBoxLayout()
        else:
            self.frame = qt.QGridLayout()
            print("Frame type not supported!")

        box.setLayout(self.frame)

class new_RectROI(pg.RectROI):
    # see https://pyqtgraph.readthedocs.io/en/latest/graphicsItems/roi.html#pyqtgraph.ROI.checkPointMove
    def __init__(self, pos, size, centered=False, sideScalers=False, **args):
        super().__init__(pos, size, centered=False, sideScalers=False, **args)

    def setBounds(self, pos, size):
        bounds = PyQt5.QtCore.QRectF(pos[0], pos[1], size[0], size[1])
        self.maxBounds = bounds

    def setEnabled(self, arg):
        if not isinstance(arg, bool):
            logging.warning("Argument given in wrong type.")
            logging.warning(traceback.format_exc())
            return

        self.resizable = arg # set if ROI can be scaled
        self.translatable = arg # set if ROi can be translated

    def checkPointMove(self, handle, pos, modifiers):
        return self.resizable


class CamThread(PyQt5.QtCore.QThread):
    signal = PyQt5.QtCore.pyqtSignal(dict)

    def __init__(self, parent):
        super().__init__()
        self.parent = parent
        self.image_type = ["background", "signal"]
        self.counter_limit = self.parent.control.num_img_to_take*len(self.image_type)
        self.counter = 0

        if self.parent.control.control_mode == "record":
            self.camera_count_list = []
            self.img_ave = np.zeros((self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]))
        elif self.parent.control.control_mode == "scan":
            self.camera_count_dict = {}

        self.parent.device.cam.record(number_of_images=4, mode='ring buffer')
        # number_of_images is buffer size in ring buffer mode, and has to be at least 4
        self.last_time = time.time()

    def run(self):
        while self.counter < self.counter_limit and self.parent.control.active:
            type = self.image_type[self.counter%2] # odd-numbered image is background, even-numbered image is signal
            self.counter += 1

            if self.parent.device.trigger_mode == "software":
                self.parent.device.cam.sdk.force_trigger() # softwarely trigger pixelfly camera
                time.sleep(0.5)

            while self.parent.control.active:
                if self.parent.device.cam.rec.get_status()['dwProcImgCount'] >= self.counter:
                    break
                time.sleep(0.001)

            if self.parent.control.active:
                image, meta = self.parent.device.cam.image(image_number=0xFFFFFFFF) # readout the lastest image
                # image is in "unit16" data type
                image = np.flip(image.T, 1).astype("float")

                if type == "background":
                    image = np.zeros(image.shape)
                    self.image_bg = image
                    self.img_dict = {}
                    self.img_dict["type"] = "background"
                    self.img_dict["image"] = image
                    self.signal.emit(self.img_dict)

                elif type == "signal":
                    image_bgsub = image - self.image_bg # "uint16" can't represent negative number
                    image_bgsub_chop = image_bgsub[self.parent.control.roi["xmin"] : self.parent.control.roi["xmax"],
                                                    self.parent.control.roi["ymin"] : self.parent.control.roi["ymax"]]
                    cc = np.sum(image_bgsub_chop)
                    num = int(self.counter/len(self.image_type))
                    self.img_dict = {}
                    self.img_dict["type"] = "signal"
                    self.img_dict["num_image"] = num
                    self.img_dict["image"] = image
                    self.img_dict["image_bgsub"] = image_bgsub
                    self.img_dict["image_bgsub_chop"] = image_bgsub_chop
                    self.img_dict["camera_count"] = np.format_float_scientific(cc, precision=4)

                    if self.parent.control.control_mode == "record":
                        self.camera_count_list.append(cc)
                        self.img_ave = np.average(np.array([self.img_ave, self.img_dict["image_bgsub"]]), axis=0, weights=[(num-1)/num, 1/num])
                        self.img_dict["image_ave"] = self.img_ave
                        self.img_dict["camera_count_ave"] = np.format_float_scientific(np.mean(self.camera_count_list), precision=4)
                        self.img_dict["camera_count_err"] = np.format_float_scientific(np.std(self.camera_count_list)/np.sqrt(num), precision=4)
                    elif self.parent.control.control_mode == "scan":
                        scan_param = self.parent.control.scan_config[f"Sequence element {num-1}"][self.parent.control.scan_param_name]
                        if scan_param in self.camera_count_dict:
                            self.camera_count_dict[scan_param] = np.append(self.camera_count_dict[scan_param], cc)
                        else:
                            self.camera_count_dict[scan_param] = np.array([cc])
                        self.img_dict["scan_param"] = scan_param
                        self.img_dict["camera_count_scan"] = self.camera_count_dict

                    self.signal.emit(self.img_dict)

                else:
                    print("Image type not supported.")

                # Not sure about the reason, but if I just update imges in the main thread from here, it sometimes work but sometimes not.
                # It seems that such signal-slot way is preferred,
                # e.g. https://stackoverflow.com/questions/54961905/real-time-plotting-using-pyqtgraph-and-threading

                print(f"image {self.counter}: "+"{:.5f} s".format(time.time()-self.last_time))

        self.parent.device.cam.stop()


class pixelfly:
    def __init__(self, parent):
        self.parent = parent

        try:
            self.cam = pco.Camera(interface='USB 2.0')
        except Exception as err:
            logging.error(traceback.format_exc())
            logging.error("Can't open camera")
            return

        self.set_sensor_format(self.parent.defaults["sensor_format"]["default"])
        self.set_clock_rate(self.parent.defaults["clock_rate"]["default"])
        self.set_conv_factor(self.parent.defaults["conv_factor"]["default"])
        self.set_trigger_mode(self.parent.defaults["trigger_mode"]["default"], True)
        self.set_expo_time(self.parent.defaults["expo_time"].getfloat("default"))
        self.set_binning(self.parent.defaults["binning"].getint("horizontal_default"),
                        self.parent.defaults["binning"].getint("vertical_default"))
        self.set_image_shape()

    def set_sensor_format(self, arg):
        self.sensor_format = arg
        format_cam = self.parent.defaults["sensor_format"][arg]
        self.cam.sdk.set_sensor_format(format_cam)
        self.cam.sdk.arm_camera()
        # print(f"sensor format = {arg}")

    def set_clock_rate(self, arg):
        rate = self.parent.defaults["clock_rate"].getint(arg)
        self.cam.configuration = {"pixel rate": rate}
        # print(f"clock rate = {arg}")

    def set_conv_factor(self, arg):
        conv = self.parent.defaults["conv_factor"].getint(arg)
        self.cam.sdk.set_conversion_factor(conv)
        self.cam.sdk.arm_camera()
        # print(f"conversion factor = {arg}")

    def set_trigger_mode(self, text, checked):
        if checked:
            self.trigger_mode = text
            mode_cam = self.parent.defaults["trigger_mode"][text]
            self.cam.configuration = {"trigger": mode_cam}
            # print(f"trigger source = {arg}")

    def set_expo_time(self, expo_time):
        self.cam.configuration = {'exposure time': expo_time}
        # print(f"exposure time (in seconds) = {expo_time}")

    def set_binning(self, bin_h, bin_v):
        self.binning = {"horizontal": int(bin_h), "vertical": int(bin_v)}
        self.cam.configuration = {'binning': (self.binning["horizontal"], self.binning["vertical"])}
        # print(f"binning = {bin_h} (horizontal), {bin_v} (vertical)")

    def set_image_shape(self):
        format_str = self.sensor_format + " absolute_"
        self.image_shape = {"xmax": int(self.parent.defaults["sensor_format"].getint(format_str+"xmax")/self.binning["horizontal"]),
                            "ymax": int(self.parent.defaults["sensor_format"].getint(format_str+"ymax")/self.binning["vertical"])}


class Control(Scrollarea):
    def __init__(self, parent):
        super().__init__(parent, label="", type="vbox")
        self.setMaximumWidth(400)
        self.frame.setContentsMargins(0,0,0,0)
        self.cpu_limit = self.parent.defaults["gaussian_fit"].getint("cpu_limit") # biggest matrix we can do gaussian fit to
        self.hdf_filename = self.parent.defaults["image_save"]["file_name"]

        self.num_img_to_take = self.parent.defaults["image_number"].getint("default")
        self.roi = {"xmin": self.parent.defaults["roi"].getint("xmin"),
                    "xmax": self.parent.defaults["roi"].getint("xmax"),
                    "ymin": self.parent.defaults["roi"].getint("ymin"),
                    "ymax": self.parent.defaults["roi"].getint("ymax")}
        self.gaussian_fit = self.parent.defaults["gaussian_fit"].getboolean("default")
        self.img_save = self.parent.defaults["image_save"].getboolean("default")

        self.active = False
        self.control_mode = None

        self.place_recording()
        self.place_image_control()
        self.place_cam_control()
        self.place_save_load()

    def place_recording(self):
        record_box = qt.QGroupBox("Recording")
        record_box.setStyleSheet("QGroupBox {border: 1px solid #304249;}")
        record_box.setMaximumHeight(270)
        record_frame = qt.QGridLayout()
        record_box.setLayout(record_frame)
        self.frame.addWidget(record_box)

        self.record_bt = qt.QPushButton("Record")
        self.record_bt.clicked[bool].connect(lambda val, mode="record": self.start(mode))
        record_frame.addWidget(self.record_bt, 0, 0)

        self.scan_bt = qt.QPushButton("Scan")
        self.scan_bt.clicked[bool].connect(lambda val, mode="scan": self.start(mode))
        record_frame.addWidget(self.scan_bt, 0, 1)

        self.stop_bt = qt.QPushButton("Stop")
        self.stop_bt.clicked[bool].connect(lambda val: self.stop())
        record_frame.addWidget(self.stop_bt, 0, 2)
        self.stop_bt.setEnabled(False)

        record_frame.addWidget(qt.QLabel("Camera count:"), 1, 0, 1, 1)
        self.camera_count = qt.QLabel()
        self.camera_count.setText("0")
        self.camera_count.setStyleSheet("QLabel{background-color: gray; font: 20pt}")
        self.camera_count.setToolTip("after background subtraction")
        record_frame.addWidget(self.camera_count, 1, 1, 1, 2)

        record_frame.addWidget(qt.QLabel("Cam. mean:"), 2, 0, 1, 1)
        self.camera_count_mean = qt.QLabel()
        self.camera_count_mean.setText("0")
        self.camera_count_mean.setStyleSheet("QLabel{background-color: gray; font: 20pt}")
        self.camera_count_mean.setToolTip("after background subtraction")
        record_frame.addWidget(self.camera_count_mean, 2, 1, 1, 2)

        record_frame.addWidget(qt.QLabel("Cam. error:"), 3, 0, 1, 1)
        self.camera_count_err_mean = qt.QLabel()
        self.camera_count_err_mean.setText("0")
        self.camera_count_err_mean.setStyleSheet("QLabel{background-color: gray; font: 20pt}")
        self.camera_count_err_mean.setToolTip("after background subtraction")
        record_frame.addWidget(self.camera_count_err_mean, 3, 1, 1, 2)

    def place_image_control(self):
        img_ctrl_box = qt.QGroupBox("Image Control")
        img_ctrl_box.setStyleSheet("QGroupBox {border: 1px solid #304249;}")
        img_ctrl_frame = qt.QFormLayout()
        img_ctrl_box.setLayout(img_ctrl_frame)
        self.frame.addWidget(img_ctrl_box)

        self.num_img_to_take_sb = qt.QSpinBox()
        num_img_upperlimit = self.parent.defaults["image_number"].getint("max")
        self.num_img_to_take_sb.setRange(1, num_img_upperlimit)
        self.num_img_to_take_sb.setValue(self.num_img_to_take)
        self.num_img_to_take_sb.valueChanged[int].connect(lambda val: self.set_num_img(val))
        img_ctrl_frame.addRow("Num of image to take:", self.num_img_to_take_sb)

        self.x_min_sb = qt.QSpinBox()
        self.x_min_sb.setRange(0, self.roi["xmax"]-1)
        self.x_min_sb.setValue(self.roi["xmin"])
        self.x_max_sb = qt.QSpinBox()
        self.x_max_sb.setRange(self.roi["xmin"]+1, self.parent.device.image_shape["xmax"])
        self.x_max_sb.setValue(self.roi["xmax"])
        self.x_min_sb.valueChanged[int].connect(lambda val, text='xmin', sb=self.x_max_sb:
                                                self.set_roi(text, val, sb))
        self.x_max_sb.valueChanged[int].connect(lambda val, text='xmax', sb=self.x_min_sb:
                                                self.set_roi(text, val, sb))

        x_range_box = qt.QWidget()
        x_range_layout = qt.QHBoxLayout()
        x_range_layout.setContentsMargins(0,0,0,0)
        x_range_box.setLayout(x_range_layout)
        x_range_layout.addWidget(self.x_min_sb)
        x_range_layout.addWidget(self.x_max_sb)
        img_ctrl_frame.addRow("ROI X range:", x_range_box)

        self.y_min_sb = qt.QSpinBox()
        self.y_min_sb.setRange(0, self.roi["ymax"]-1)
        self.y_min_sb.setValue(self.roi["ymin"])
        self.y_max_sb = qt.QSpinBox()
        self.y_max_sb.setRange(self.roi["ymin"]+1, self.parent.device.image_shape["ymax"])
        self.y_max_sb.setValue(self.roi["ymax"])
        self.y_min_sb.valueChanged[int].connect(lambda val, text='ymin', sb=self.y_max_sb:
                                                self.set_roi(text, val, sb))
        self.y_max_sb.valueChanged[int].connect(lambda val, text='ymax', sb=self.y_min_sb:
                                                self.set_roi(text, val, sb))

        y_range_box = qt.QWidget()
        y_range_layout = qt.QHBoxLayout()
        y_range_layout.setContentsMargins(0,0,0,0)
        y_range_box.setLayout(y_range_layout)
        y_range_layout.addWidget(self.y_min_sb)
        y_range_layout.addWidget(self.y_max_sb)
        img_ctrl_frame.addRow("ROI Y range:", y_range_box)

        self.num_image = qt.QLabel()
        self.num_image.setText("0")
        self.num_image.setStyleSheet("background-color: gray;")
        img_ctrl_frame.addRow("Num of recorded images:", self.num_image)

        # self.image_width = qt.QLabel()
        # self.image_width.setText("0")
        # self.image_width.setStyleSheet("background-color: gray;")
        # self.image_height = qt.QLabel()
        # self.image_height.setText("0")
        # self.image_height.setStyleSheet("background-color: gray;")
        # image_shape_box = qt.QWidget()
        # image_shape_layout = qt.QHBoxLayout()
        # image_shape_layout.setContentsMargins(0,0,0,0)
        # image_shape_box.setLayout(image_shape_layout)
        # image_shape_layout.addWidget(self.image_width)
        # image_shape_layout.addWidget(self.image_height)
        # img_ctrl_frame.addRow("Image width x height:", image_shape_box)

        self.gauss_fit_chb = qt.QCheckBox()
        self.gauss_fit_chb.setTristate(False)
        self.gauss_fit_chb.setChecked(self.gaussian_fit)
        self.gauss_fit_chb.setStyleSheet("QCheckBox::indicator {width: 15px; height: 15px;}")
        self.gauss_fit_chb.stateChanged[int].connect(lambda state: self.set_gauss_fit(state))
        self.gauss_fit_chb.setToolTip(f"Can only be enabled when image size less than {self.cpu_limit} pixels.")
        img_ctrl_frame.addRow("2D gaussian fit:", self.gauss_fit_chb)

        if (self.roi["xmax"]-self.roi["xmin"])*(self.roi["ymax"]-self.roi["ymin"]) > self.cpu_limit:
            # this line has to be after gauss_fit_chb's connect()
            self.gauss_fit_chb.setChecked(False)
            self.gauss_fit_chb.setEnabled(False)

        self.img_save_chb = qt.QCheckBox()
        self.img_save_chb.setTristate(False)
        self.img_save_chb.setChecked(self.img_save)
        self.img_save_chb.setStyleSheet("QCheckBox::indicator {width: 15px; height: 15px;}")
        self.img_save_chb.stateChanged[int].connect(lambda state: self.set_img_save(state))
        img_ctrl_frame.addRow("Image auto save:", self.img_save_chb)

        img_ctrl_frame.addRow("------------------", qt.QWidget())

        self.x_mean = qt.QLabel()
        self.x_mean.setMaximumWidth(90)
        self.x_mean.setText("0")
        self.x_mean.setStyleSheet("QWidget{background-color: gray;}")
        self.x_mean.setToolTip("x mean")
        self.x_stand_dev = qt.QLabel()
        self.x_stand_dev.setMaximumWidth(90)
        self.x_stand_dev.setText("0")
        self.x_stand_dev.setStyleSheet("QWidget{background-color: gray;}")
        self.x_stand_dev.setToolTip("x standard deviation")
        gauss_x_box = qt.QWidget()
        gauss_x_layout = qt.QHBoxLayout()
        gauss_x_layout.setContentsMargins(0,0,0,0)
        gauss_x_box.setLayout(gauss_x_layout)
        gauss_x_layout.addWidget(self.x_mean)
        gauss_x_layout.addWidget(self.x_stand_dev)
        img_ctrl_frame.addRow("2D gaussian fit (x):", gauss_x_box)

        self.y_mean = qt.QLabel()
        self.y_mean.setMaximumWidth(90)
        self.y_mean.setText("0")
        self.y_mean.setStyleSheet("QWidget{background-color: gray;}")
        self.y_mean.setToolTip("y mean")
        self.y_stand_dev = qt.QLabel()
        self.y_stand_dev.setMaximumWidth(90)
        self.y_stand_dev.setText("0")
        self.y_stand_dev.setStyleSheet("QWidget{background-color: gray;}")
        self.y_stand_dev.setToolTip("y standard deviation")
        gauss_y_box = qt.QWidget()
        gauss_y_layout = qt.QHBoxLayout()
        gauss_y_layout.setContentsMargins(0,0,0,0)
        gauss_y_box.setLayout(gauss_y_layout)
        gauss_y_layout.addWidget(self.y_mean)
        gauss_y_layout.addWidget(self.y_stand_dev)
        img_ctrl_frame.addRow("2D gaussian fit (y):", gauss_y_box)

        self.amp = qt.QLabel()
        self.amp.setText("0")
        self.amp.setStyleSheet("QWidget{background-color: gray;}")
        img_ctrl_frame.addRow("2D gaussian fit (amp.):", self.amp)

        self.offset = qt.QLabel()
        self.offset.setText("0")
        self.offset.setStyleSheet("QWidget{background-color: gray;}")
        img_ctrl_frame.addRow("2D gaussian fit (offset):", self.offset)

    def place_cam_control(self):
        self.cam_ctrl_box = qt.QGroupBox("Camera Control")
        self.cam_ctrl_box.setStyleSheet("QGroupBox {border: 1px solid #304249;}")
        cam_ctrl_frame = qt.QFormLayout()
        self.cam_ctrl_box.setLayout(cam_ctrl_frame)
        self.frame.addWidget(self.cam_ctrl_box)

        self.sensor_format_cb = qt.QComboBox()
        self.sensor_format_cb.setMaximumWidth(200)
        self.sensor_format_cb.setMaximumHeight(20)
        op = [x.strip() for x in self.parent.defaults["sensor_format"]["options"].split(',')]
        for i in op:
            self.sensor_format_cb.addItem(i)
        self.sensor_format_cb.setCurrentText(self.parent.device.sensor_format)
        self.sensor_format_cb.currentTextChanged[str].connect(lambda val: self.set_sensor_format(val))
        cam_ctrl_frame.addRow("Sensor format:", self.sensor_format_cb)

        self.clock_rate_cb = qt.QComboBox()
        self.clock_rate_cb.setMaximumWidth(200)
        self.clock_rate_cb.setMaximumHeight(20)
        op = [x.strip() for x in self.parent.defaults["clock_rate"]["options"].split(',')]
        for i in op:
            self.clock_rate_cb.addItem(i)
        default = self.parent.defaults["clock_rate"]["default"]
        self.clock_rate_cb.setCurrentText(default)
        self.clock_rate_cb.currentTextChanged[str].connect(lambda val: self.parent.device.set_clock_rate(val))
        cam_ctrl_frame.addRow("Clock rate:", self.clock_rate_cb)

        self.conv_factor_cb = qt.QComboBox()
        self.conv_factor_cb.setMaximumWidth(200)
        self.conv_factor_cb.setMaximumHeight(20)
        self.conv_factor_cb.setToolTip("1/gain, or electrons/count")
        op = [x.strip() for x in self.parent.defaults["conv_factor"]["options"].split(',')]
        for i in op:
            self.conv_factor_cb.addItem(i)
        default = self.parent.defaults["conv_factor"]["default"]
        self.conv_factor_cb.setCurrentText(default)
        self.conv_factor_cb.currentTextChanged[str].connect(lambda val: self.parent.device.set_conv_factor(val))
        cam_ctrl_frame.addRow("Conversion factor:", self.conv_factor_cb)

        self.trig_mode_rblist = []
        trig_bg = qt.QButtonGroup(self.parent)
        self.trig_box = qt.QWidget()
        self.trig_box.setMaximumWidth(200)
        trig_layout = qt.QHBoxLayout()
        trig_layout.setContentsMargins(0,0,0,0)
        self.trig_box.setLayout(trig_layout)
        op = [x.strip() for x in self.parent.defaults["trigger_mode"]["options"].split(',')]
        for i in op:
            trig_mode_rb = qt.QRadioButton(i)
            trig_mode_rb.setChecked(True if i == self.parent.device.trigger_mode else False)
            trig_mode_rb.toggled[bool].connect(lambda val, rb=trig_mode_rb: self.parent.device.set_trigger_mode(rb.text(), val))
            self.trig_mode_rblist.append(trig_mode_rb)
            trig_bg.addButton(trig_mode_rb)
            trig_layout.addWidget(trig_mode_rb)
        cam_ctrl_frame.addRow("Trigger mode:", self.trig_box)

        self.expo_le = qt.QLineEdit() # try qt.QDoubleSpinBox() ?
        default = self.parent.defaults["expo_time"].getfloat("default")
        default_unit = self.parent.defaults["expo_unit"]["default"]
        default_unit_num = self.parent.defaults["expo_unit"].getfloat(default_unit)
        default_time = str(default/default_unit_num)
        self.expo_le.setText(default_time)
        self.expo_unit_cb = qt.QComboBox()
        self.expo_unit_cb.setMaximumHeight(30)
        op = [x.strip() for x in self.parent.defaults["expo_unit"]["options"].split(',')]
        for i in op:
            self.expo_unit_cb.addItem(i)
        self.expo_unit_cb.setCurrentText(default_unit)
        self.expo_le.editingFinished.connect(lambda le=self.expo_le, cb=self.expo_unit_cb:
                                            self.set_expo_time(le.text(), cb.currentText()))
        self.expo_unit_cb.currentTextChanged[str].connect(lambda val, le=self.expo_le: self.set_expo_time(le.text(), val))
        expo_box = qt.QWidget()
        expo_box.setMaximumWidth(200)
        expo_layout = qt.QHBoxLayout()
        expo_layout.setContentsMargins(0,0,0,0)
        expo_box.setLayout(expo_layout)
        expo_layout.addWidget(self.expo_le)
        expo_layout.addWidget(self.expo_unit_cb)
        cam_ctrl_frame.addRow("Exposure time:", expo_box)

        self.bin_hori_cb = qt.QComboBox()
        self.bin_vert_cb = qt.QComboBox()
        op = [x.strip() for x in self.parent.defaults["binning"]["options"].split(',')]
        for i in op:
            self.bin_hori_cb.addItem(i)
            self.bin_vert_cb.addItem(i)
        self.bin_hori_cb.setCurrentText(str(self.parent.device.binning["horizontal"]))
        self.bin_vert_cb.setCurrentText(str(self.parent.device.binning["vertical"]))
        self.bin_hori_cb.currentTextChanged[str].connect(lambda val, text="hori", cb=self.bin_vert_cb: self.set_binning(text, val, cb.currentText()))
        self.bin_vert_cb.currentTextChanged[str].connect(lambda val, text="vert", cb=self.bin_hori_cb: self.set_binning(text, cb.currentText(), val))
        bin_box = qt.QWidget()
        bin_box.setMaximumWidth(200)
        bin_layout = qt.QHBoxLayout()
        bin_layout.setContentsMargins(0,0,0,0)
        bin_box.setLayout(bin_layout)
        bin_layout.addWidget(self.bin_hori_cb)
        bin_layout.addWidget(self.bin_vert_cb)
        cam_ctrl_frame.addRow("Binning H x V:", bin_box)

    def place_save_load(self):
        self.save_load_box = qt.QGroupBox("Save/Load Settings")
        self.save_load_box.setStyleSheet("QGroupBox {border: 1px solid #304249;}")
        save_load_frame = qt.QFormLayout()
        self.save_load_box.setLayout(save_load_frame)
        self.frame.addWidget(self.save_load_box)

        self.file_name_le = qt.QLineEdit()
        default_file_name = self.parent.defaults["setting_save"]["file_name"]
        self.file_name_le.setText(default_file_name)
        save_load_frame.addRow("File name to save:", self.file_name_le)

        self.date_time_chb = qt.QCheckBox()
        self.date_time_chb.setTristate(False)
        date = self.parent.defaults["setting_save"].getboolean("append_time")
        self.date_time_chb.setChecked(date)
        self.date_time_chb.setStyleSheet("QCheckBox::indicator {width: 15px; height: 15px;}")
        save_load_frame.addRow("Auto append time:", self.date_time_chb)

        self.save_settings_bt = qt.QPushButton("save settings")
        self.save_settings_bt.setMaximumWidth(200)
        self.save_settings_bt.clicked[bool].connect(lambda val: self.save_settings())
        save_load_frame.addRow("Save settings:", self.save_settings_bt)

        self.load_settings_bt = qt.QPushButton("load settings")
        self.load_settings_bt.setMaximumWidth(200)
        self.load_settings_bt.clicked[bool].connect(lambda val: self.load_settings())
        save_load_frame.addRow("Load settings:", self.load_settings_bt)

    def start(self, mode):
        self.control_mode = mode
        self.active = True

        # clear camera count QLabels
        self.camera_count.setText("0")
        self.camera_count_mean.setText("0")
        self.camera_count_err_mean.setText("0")
        self.num_image.setText("0")

        # clear images
        img = np.zeros((self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]))
        for key, image_show in self.parent.image_win.imgs_dict.items():
            image_show.setImage(img)
        self.parent.image_win.x_plot_curve.setData(np.sum(img, axis=1))
        self.parent.image_win.y_plot_curve.setData(np.sum(img, axis=0))
        self.parent.image_win.ave_img.setImage(img)

        # clear gaussian fit QLabels
        self.amp.setText("0")
        self.offset.setText("0")
        self.x_mean.setText("0")
        self.x_stand_dev.setText("0")
        self.y_mean.setText("0")
        self.y_stand_dev.setText("0")

        if self.img_save:
            with h5py.File(self.hdf_filename, "a") as hdf_file:
                self.hdf_group_name = "run_"+time.strftime("%Y%m%d_%H%M%S")
                hdf_file.create_group(self.hdf_group_name)

        if self.control_mode == "scan":
            self.scan_config = configparser.ConfigParser()
            self.scan_config.read(self.parent.defaults["scan_file_name"]["default"])
            self.num_img_to_take_sb.setValue(self.scan_config["Settings"].getint("element number"))
            # self.num_img_to_take will be changed automatically
            self.scan_param_name = self.scan_config["Settings"].get("scan param name")
            self.scan_device = self.scan_config["Settings"].get("scan device")
            self.parent.image_win.scan_plot_widget.setLabel("bottom", self.scan_device+" "+self.scan_param_name, units=self.scan_config["Settings"].get("unit"))

        self.enable_widgets(False)

        self.rec = CamThread(self.parent)
        self.rec.signal.connect(self.img_ctrl_update)
        self.rec.finished.connect(self.stop)
        self.rec.start()

        # Another way to do this is to use QTimer() to trigger imgae image readout,
        # but in that case, the while loop that waits for the image is running in the main thread,
        # so it will block the main thread.

    def stop(self):
        if self.active:
            self.active = False
            self.control_mode = None
            self.enable_widgets(True)

    @PyQt5.QtCore.pyqtSlot(dict)
    def img_ctrl_update(self, img_dict):
        if img_dict["type"] == "background":
            img = img_dict["image"]
            self.parent.image_win.imgs_dict["Background"].setImage(img)

        elif img_dict["type"] == "signal":
            img = img_dict["image"]
            self.parent.image_win.imgs_dict["Signal"].setImage(img)
            img = img_dict["image_bgsub"]
            self.parent.image_win.imgs_dict["Signal w/ bg subtraction"].setImage(img)
            self.parent.image_win.x_plot_curve.setData(np.sum(img, axis=1))
            self.parent.image_win.y_plot_curve.setData(np.sum(img, axis=0))
            self.num_image.setText(str(img_dict["num_image"]))
            self.camera_count.setText(str(img_dict["camera_count"]))

            if self.control_mode == "record":
                self.parent.image_win.ave_img.setImage(img_dict["image_ave"])
                self.camera_count_mean.setText(str(img_dict["camera_count_ave"]))
                self.camera_count_err_mean.setText(str(img_dict["camera_count_err"]))
            elif self.control_mode == "scan":
                x = np.array([])
                y = np.array([])
                err = np.array([])
                for i, (param, cc_list) in enumerate(img_dict["camera_count_scan"].items()):
                    x = np.append(x, float(param))
                    y = np.append(y, np.mean(cc_list))
                    err = np.append(err, np.std(cc_list)/np.sqrt(len(cc_list)))
                order = x.argsort()
                x = x[order]
                y = y[order]
                err = err[order]
                self.parent.image_win.scan_plot_curve.setData(x, y, symbol='o')
                self.parent.image_win.scan_plot_errbar.setData(x=x, y=y, top=err, bottom=err, beam=(x[-1]-x[0])/len(x)*0.2, pen=pg.mkPen('w', width=1.2))

            if self.gaussian_fit:
                param = gaussianfit(img_dict["image_bgsub_chop"])
                self.amp.setText("{:.2f}".format(param["amp"]))
                self.offset.setText("{:.2f}".format(param["offset"]))
                self.x_mean.setText("{:.2f}".format(param["x_mean"]+self.roi["xmin"]))
                self.x_stand_dev.setText("{:.2f}".format(param["x_width"]))
                self.y_mean.setText("{:.2f}".format(param["y_mean"]+self.roi["ymin"]))
                self.y_stand_dev.setText("{:.2f}".format(param["y_width"]))

            if self.img_save:
                with h5py.File(self.hdf_filename, "r+") as hdf_file:
                    root = hdf_file.require_group(self.hdf_group_name)
                    if self.control_mode == "scan":
                        root.attrs["scanned parameter"] = self.scan_device+" "+self.scan_param_name
                        root.attrs["number of images"] = self.num_img_to_take
                        root = root.require_group(self.scan_param_name+"_"+img_dict["scan_param"])
                        root.attrs["scanned parameter"] = self.scan_device+" "+self.scan_param_name
                        root.attrs["scanned param value"] = img_dict["scan_param"]
                        root.attrs["scanned param unit"] = self.scan_config["Settings"].get("unit")
                    dset = root.create_dataset(
                                    name                 = "image" + "_{:06d}".format(img_dict["num_image"]),
                                    data                 = img_dict["image_bgsub_chop"],
                                    shape                = img_dict["image_bgsub_chop"].shape,
                                    dtype                = "f",
                                    compression          = "gzip",
                                    compression_opts     = 4
                                )
                    dset.attrs["camera count"] = img_dict["camera_count"]
                    if self.gaussian_fit:
                        for key, val in param.items():
                            dset.attrs["2D gaussian fit"+key] = val

    def enable_widgets(self, arg):
        self.stop_bt.setEnabled(not arg)
        self.record_bt.setEnabled(arg)
        self.scan_bt.setEnabled(arg)
        self.num_img_to_take_sb.setEnabled(arg)
        self.x_min_sb.setEnabled(arg)
        self.x_max_sb.setEnabled(arg)
        self.y_min_sb.setEnabled(arg)
        self.y_max_sb.setEnabled(arg)
        self.gauss_fit_chb.setEnabled(arg)
        self.img_save_chb.setEnabled(arg)
        self.cam_ctrl_box.setEnabled(arg)
        self.save_load_box.setEnabled(arg)

        for key, roi in self.parent.image_win.img_roi_dict.items():
            roi.setEnabled(arg)
        self.parent.image_win.x_plot_lr.setMovable(arg)
        self.parent.image_win.y_plot_lr.setMovable(arg)

        self.parent.app.processEvents()

    def set_num_img(self, val):
        self.num_img_to_take = val

    def set_roi(self, text, val, sb):
        if text == "xmin":
            sb.setMinimum(val+1)
        elif text == "xmax":
            sb.setMaximum(val-1)
        elif text == "ymin":
            sb.setMinimum(val+1)
        elif text == "ymax":
            sb.setMaximum(val-1)

        self.roi[text] = val

        x_range = self.roi["xmax"]-self.roi["xmin"]
        y_range = self.roi["ymax"]-self.roi["ymin"]
        for key, roi in self.parent.image_win.img_roi_dict.items():
            roi.setPos(pos=(self.roi["xmin"], self.roi["ymin"]))
            roi.setSize(size=(x_range, y_range))

        self.parent.image_win.x_plot_lr.setRegion((self.roi["xmin"], self.roi["xmax"]))
        self.parent.image_win.y_plot_lr.setRegion((self.roi["ymin"], self.roi["ymax"]))

        if x_range*y_range > self.cpu_limit:
            if self.gauss_fit_chb.isEnabled():
                self.gauss_fit_chb.setChecked(False)
                self.gauss_fit_chb.setEnabled(False)
        else:
            if not self.gauss_fit_chb.isEnabled():
                self.gauss_fit_chb.setEnabled(True)

    def set_gauss_fit(self, state):
        self.gaussian_fit = state

    def set_img_save(self, state):
        self.img_save = state

    def set_sensor_format(self, val):
        format_str = val + " absolute_"
        x_max = (self.parent.defaults["sensor_format"].getint(format_str+"xmax"))/self.parent.device.binning["horizontal"]
        self.x_max_sb.setMaximum(int(x_max))
        y_max = (self.parent.defaults["sensor_format"].getint(format_str+"ymax"))/self.parent.device.binning["vertical"]
        self.y_max_sb.setMaximum(int(y_max))
        # number in both 'min' and 'max' spinboxes will adjusted automatically

        self.parent.device.set_sensor_format(val)
        self.parent.device.set_image_shape()

        for key, roi in self.parent.image_win.img_roi_dict.items():
            roi.setBounds(pos=[0,0], size=[self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]])
        self.parent.image_win.x_plot_lr.setBounds([0, self.parent.device.image_shape["xmax"]])
        self.parent.image_win.y_plot_lr.setBounds([0, self.parent.device.image_shape["ymax"]])

    def set_expo_time(self, time, unit):
        unit_num = self.parent.defaults["expo_unit"].getfloat(unit)
        try:
            expo_time = float(time)*unit_num
        except ValueError as err:
            logging.warning(traceback.format_exc())
            logging.warning("Exposure time invalid!")
            return

        expo_decimals = self.parent.defaults["expo_time"].getint("decimals")
        expo_min = self.parent.defaults["expo_time"].getfloat("min")
        expo_max = self.parent.defaults["expo_time"].getfloat("max")
        expo_time_round = round(expo_time, expo_decimals)
        if expo_time_round < expo_min:
            expo_time_round = expo_min
        elif expo_time_round > expo_max:
            expo_time_round = expo_max

        d = int(expo_decimals+np.log10(unit_num))
        if d:
            t = round(expo_time_round/unit_num, d)
            t = f"{t}"
        else:
            t = "{:d}".format(round(expo_time_round/unit_num))

        self.expo_le.setText(t)
        self.parent.device.set_expo_time(expo_time_round)

    def set_binning(self, text, bin_h, bin_v):
        format_str = self.parent.device.sensor_format + " absolute_"
        if text == "hori":
            x_max = (self.parent.defaults["sensor_format"].getint(format_str+"xmax"))/int(bin_h)
            self.x_max_sb.setMaximum(int(x_max))
        elif text == "vert":
            y_max = (self.parent.defaults["sensor_format"].getint(format_str+"ymax"))/int(bin_v)
            self.y_max_sb.setMaximum(int(y_max))
        else:
            print("Binning type not supported.")

        self.parent.device.set_binning(bin_h, bin_v)
        self.parent.device.set_image_shape()

        for key, roi in self.parent.image_win.img_roi_dict.items():
            roi.setBounds(pos=[0,0], size=[self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]])
        self.parent.image_win.x_plot_lr.setBounds([0, self.parent.device.image_shape["xmax"]])
        self.parent.image_win.y_plot_lr.setBounds([0, self.parent.device.image_shape["ymax"]])

    def save_settings(self):
        file_name = ""
        if self.file_name_le.text():
            file_name += self.file_name_le.text()
        if self.date_time_chb.isChecked():
            if file_name != "":
                file_name += "_"
            file_name += time.strftime("%Y%m%d_%H%M%S")
        file_name += ".ini"
        file_name = r"saved_settings/"+file_name
        if os.path.exists(file_name):
            overwrite = qt.QMessageBox.warning(self, 'File name exists',
                                            'File name already exists. Continue to overwrite it?',
                                            qt.QMessageBox.Yes | qt.QMessageBox.No,
                                            qt.QMessageBox.No)
            if overwrite == qt.QMessageBox.No:
                return

        config = configparser.ConfigParser()

        config["image_control"] = {}
        config["image_control"]["num_image"] = str(self.num_img_to_take_sb.value())
        config["image_control"]["xmin"] = str(self.x_min_sb.value())
        config["image_control"]["xmax"] = str(self.x_max_sb.value())
        config["image_control"]["ymin"] = str(self.y_min_sb.value())
        config["image_control"]["ymax"] = str(self.y_max_sb.value())
        config["image_control"]["2D_gaussian_fit"] = str(self.gauss_fit_chb.isChecked())
        config["image_control"]["image_auto_save"] = str(self.img_save_chb.isChecked())

        config["camera_control"] = {}
        config["camera_control"]["sensor_format"] = self.sensor_format_cb.currentText()
        config["camera_control"]["clock_rate"] = self.clock_rate_cb.currentText()
        config["camera_control"]["conversion_factor"] = self.conv_factor_cb.currentText()
        for i in self.trig_mode_rblist:
            if i.isChecked():
                t = i.text()
                break
        config["camera_control"]["trigger_mode"] = t
        config["camera_control"]["exposure_time"] = self.expo_le.text()
        config["camera_control"]["exposure_unit"] = self.expo_unit_cb.currentText()
        config["camera_control"]["binning_horizontal"] = self.bin_hori_cb.currentText()
        config["camera_control"]["binning_vertical"] = self.bin_vert_cb.currentText()

        configfile = open(file_name, "w")
        config.write(configfile)
        configfile.close()

    def load_settings(self):
        file_name, _ = qt.QFileDialog.getOpenFileName(self,"Load settigns", "saved_settings/", "All Files (*);;INI File (*.ini)")
        if not file_name:
            return

        config = configparser.ConfigParser()
        config.read(file_name)

        self.num_img_to_take_sb.setValue(config["image_control"].getint("num_image"))
        # the spinbox emits 'valueChanged' signal, and its connected function will be called
        self.x_min_sb.setValue(config["image_control"].getint("xmin"))
        self.x_max_sb.setValue(config["image_control"].getint("xmax"))
        self.y_min_sb.setValue(config["image_control"].getint("ymin"))
        self.y_max_sb.setValue(config["image_control"].getint("ymax"))
        # make sure image range is updated BEFORE gauss_fit_chb
        self.gauss_fit_chb.setChecked(config["image_control"].getboolean("2d_gaussian_fit"))
        # the combobox emits 'stateChanged' signal, and its connected function will be called
        self.img_save_chb.setChecked(config["image_control"].getboolean("image_auto_save"))

        self.sensor_format_cb.setCurrentText(config["camera_control"]["sensor_format"])
        # the combobox emits 'currentTextChanged' signal, and its connected function will be called
        # make sure sensor format is updated after image range settings
        self.clock_rate_cb.setCurrentText(config["camera_control"]["clock_rate"])
        self.conv_factor_cb.setCurrentText(config["camera_control"]["conversion_factor"])
        for i in self.trig_mode_rblist:
            if i.text() == config["camera_control"]["trigger_mode"]:
                i.setChecked(True)
                break
        self.expo_le.setText(config["camera_control"]["exposure_time"])
        # QLineEdit won't emit 'editingfinishede signal
        self.expo_unit_cb.setCurrentText(config["camera_control"]["exposure_unit"])
        # make sure exposure unit is updated after exposure time QLineEdit, so the pixelfly.set_expo_time functioni will be called
        self.bin_hori_cb.setCurrentText(config["camera_control"].get("binning_horizontal"))
        self.bin_vert_cb.setCurrentText(config["camera_control"].get("binning_vertical"))
        # make sure binning is updated after image range settings

class ImageWin(Scrollarea):
    def __init__(self, parent):
        super().__init__(parent, label="Images", type="grid")
        self.colormap = steal_colormap()
        self.frame.setColumnStretch(0,7)
        self.frame.setColumnStretch(1,4)
        self.frame.setRowStretch(0,1)
        self.frame.setRowStretch(1,1)
        self.frame.setRowStretch(2,1)
        self.imgs_dict = {}
        self.img_roi_dict ={}
        self.imgs_name = ["Background", "Signal", "Signal w/ bg subtraction"]

        self.place_sgn_imgs()
        self.place_axis_plots()
        self.place_ave_image()
        self.place_scan_plot()

    def place_sgn_imgs(self):
        self.img_tab = qt.QTabWidget()
        self.frame.addWidget(self.img_tab, 0, 0, 2, 1)
        for i, name in enumerate(self.imgs_name):
            graphlayout = pg.GraphicsLayoutWidget(parent=self, border=True)
            self.img_tab.addTab(graphlayout, " "+name+" ")
            plot = graphlayout.addPlot(title=name)
            img = pg.ImageItem(lockAspect=True)
            plot.addItem(img)

            img_roi = new_RectROI(pos = [self.parent.defaults["roi"].getint("xmin"),
                                         self.parent.defaults["roi"].getint("ymin")],
                                  size = [self.parent.defaults["roi"].getint("xmax")-self.parent.defaults["roi"].getint("xmin"),
                                          self.parent.defaults["roi"].getint("ymax")-self.parent.defaults["roi"].getint("ymin")],
                                  snapSize = 0,
                                  scaleSnap = False,
                                  translateSnap = False,
                                  pen = "r")
                                  # params ([x_start, y_start], [x_span, y_span])
            img_roi.addScaleHandle([0, 0], [1, 1])
            # params ([x, y], [x position scaled around, y position scaled around]), rectangular from 0 to 1
            img_roi.addScaleHandle([0, 0.5], [1, 0.5])
            img_roi.addScaleHandle([1, 0.5], [0, 0.5])
            img_roi.addScaleHandle([0.5, 0], [0.5, 1])
            img_roi.addScaleHandle([0.5, 1], [0.5, 0])
            plot.addItem(img_roi)
            img_roi.sigRegionChanged.connect(lambda roi=img_roi: self.img_roi_update(roi))
            img_roi.setBounds(pos=[0,0], size=[self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]])
            self.img_roi_dict[name] = img_roi

            hist = pg.HistogramLUTItem()
            hist.setImageItem(img)
            graphlayout.addItem(hist)
            hist.gradient.restoreState({'mode': 'rgb', 'ticks': self.colormap})

            self.data = fake_data(self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"])
            img.setImage(self.data)

            self.imgs_dict[name] = img

        self.img_tab.setCurrentIndex(2) # make tab #2 (count from 0) to show as default

    def place_axis_plots(self):
        tickstyle = {"showValues": False}

        x_data = np.sum(self.data, axis=1)
        graphlayout = pg.GraphicsLayoutWidget(parent=self, border=True)
        self.frame.addWidget(graphlayout, 0, 1, 2, 1)
        x_plot = graphlayout.addPlot(title="Camera count v.s. X")
        x_plot.showGrid(True, True)
        x_plot.setLabel("top")
        # x_plot.getAxis("top").setTicks([])
        x_plot.getAxis("top").setStyle(**tickstyle)
        x_plot.setLabel("right")
        # x_plot.getAxis("right").setTicks([])
        x_plot.getAxis("right").setStyle(**tickstyle)
        self.x_plot_curve = x_plot.plot(x_data)
        self.x_plot_lr = pg.LinearRegionItem([self.parent.defaults["roi"].getint("xmin"),
                                                self.parent.defaults["roi"].getint("xmax")], swapMode="block")
        # no "snap" option for LinearRegion item?
        self.x_plot_lr.setBounds([0, self.parent.device.image_shape["xmax"]])
        x_plot.addItem(self.x_plot_lr)
        self.x_plot_lr.sigRegionChanged.connect(self.x_plot_lr_update)

        graphlayout.nextRow()
        y_data = np.sum(self.data, axis=0)
        y_plot = graphlayout.addPlot(title="Camera count v.s. Y")
        y_plot.showGrid(True, True)
        y_plot.setLabel("top")
        y_plot.getAxis("top").setStyle(**tickstyle)
        y_plot.setLabel("right")
        y_plot.getAxis("right").setStyle(**tickstyle)
        self.y_plot_curve = y_plot.plot(y_data)
        self.y_plot_lr = pg.LinearRegionItem([self.parent.defaults["roi"].getint("ymin"),
                                                self.parent.defaults["roi"].getint("ymax")], swapMode="block")
        self.y_plot_lr.setBounds([0, self.parent.device.image_shape["ymax"]])
        y_plot.addItem(self.y_plot_lr)
        self.y_plot_lr.sigRegionChanged.connect(self.y_plot_lr_update)

    def place_ave_image(self):
        graphlayout = pg.GraphicsLayoutWidget(parent=self, border=True)
        self.frame.addWidget(graphlayout, 2, 0)
        plot = graphlayout.addPlot(title="Average image")
        self.ave_img = pg.ImageItem()
        plot.addItem(self.ave_img)
        hist = pg.HistogramLUTItem()
        hist.setImageItem(self.ave_img)
        graphlayout.addItem(hist)
        hist.gradient.restoreState({'mode': 'rgb', 'ticks': self.colormap})

        self.ave_img.setImage(fake_data(self.parent.device.image_shape["xmax"], self.parent.device.image_shape["ymax"]))

    def place_scan_plot(self):
        tickstyle = {"showValues": False}

        self.scan_plot_widget = pg.PlotWidget(title="Camera count v.s. Scan param.")
        self.scan_plot_widget.showGrid(True, True)
        self.scan_plot_widget.setLabel("top")
        self.scan_plot_widget.getAxis("top").setStyle(**tickstyle)
        self.scan_plot_widget.setLabel("right")
        self.scan_plot_widget.getAxis("right").setStyle(**tickstyle)
        fontstyle = {"color": "#919191", "font-size": "11pt"}
        self.scan_plot_widget.setLabel("bottom", "Scan parameter", **fontstyle)
        self.scan_plot_widget.getAxis("bottom").enableAutoSIPrefix(False)
        self.scan_plot_curve = self.scan_plot_widget.plot()
        self.scan_plot_errbar = pg.ErrorBarItem()
        self.scan_plot_widget.addItem(self.scan_plot_errbar)
        self.frame.addWidget(self.scan_plot_widget, 2, 1)

    def img_roi_update(self, roi):
        x_min = roi.pos()[0]
        y_min = roi.pos()[1]
        x_max = x_min + roi.size()[0]
        y_max = y_min + roi.size()[1]

        self.parent.control.x_min_sb.setValue(round(x_min))
        self.parent.control.x_max_sb.setValue(round(x_max))
        self.parent.control.y_min_sb.setValue(round(y_min))
        self.parent.control.y_max_sb.setValue(round(y_max))

    def x_plot_lr_update(self):
        x_min = self.x_plot_lr.getRegion()[0]
        x_max = self.x_plot_lr.getRegion()[1]

        self.parent.control.x_min_sb.setValue(round(x_min))
        self.parent.control.x_max_sb.setValue(round(x_max))

    def y_plot_lr_update(self):
        y_min = self.y_plot_lr.getRegion()[0]
        y_max = self.y_plot_lr.getRegion()[1]

        self.parent.control.y_min_sb.setValue(round(y_min))
        self.parent.control.y_max_sb.setValue(round(y_max))


class CameraGUI(qt.QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.setWindowTitle('pco.pixelfly usb (ring buffer)')
        self.setStyleSheet("QWidget{font: 10pt;}")
        # self.setStyleSheet("QToolTip{background-color: black; color: white; font: 10pt;}")
        self.app = app

        self.defaults = configparser.ConfigParser()
        self.defaults.read('defaults.ini')

        self.device = pixelfly(self)
        self.control = Control(self)
        self.image_win = ImageWin(self)

        self.splitter = qt.QSplitter()
        self.splitter.setOrientation(PyQt5.QtCore.Qt.Horizontal)
        self.setCentralWidget(self.splitter)
        self.splitter.addWidget(self.image_win)
        self.splitter.addWidget(self.control)

        self.resize(1600, 900)
        self.show()


if __name__ == '__main__':
    app = qt.QApplication(sys.argv)
    app.setStyleSheet(qdarkstyle.load_stylesheet_pyqt5())
    main_window = CameraGUI(app)
    app.exec_()
    main_window.device.cam.close()
    sys.exit(0)
