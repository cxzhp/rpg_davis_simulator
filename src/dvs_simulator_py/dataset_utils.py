#!/usr/bin/env python

import yaml
import cv2
import OpenEXR
import Imath
import numpy as np
from math import fabs, sqrt

# epsilon for testing whether a number is close to zero
_EPS = np.finfo(float).eps * 4.0

def matrix_from_quaternion(quaternion):
    """Return homogeneous rotation matrix from quaternion.

    >>> R = quaternion_matrix([0.06146124, 0, 0, 0.99810947])
    >>> numpy.allclose(R, rotation_matrix(0.123, (1, 0, 0)))
    True

    """
    q = np.array(quaternion[:4], dtype=np.float64, copy=True)
    nq = np.dot(q, q)
    if nq < _EPS:
        return np.identity(4)
    q *= sqrt(2.0 / nq)
    q = np.outer(q, q)
    return np.array((
        (1.0-q[1, 1]-q[2, 2],     q[0, 1]-q[2, 3],     q[0, 2]+q[1, 3], 0.0),
        (    q[0, 1]+q[2, 3], 1.0-q[0, 0]-q[2, 2],     q[1, 2]-q[0, 3], 0.0),
        (    q[0, 2]-q[1, 3],     q[1, 2]+q[0, 3], 1.0-q[0, 0]-q[1, 1], 0.0),
        (                0.0,                 0.0,                 0.0, 1.0)
        ), dtype=np.float64)


def logmap_so3(R):
    """Logmap at the identity.
    Returns canonical coordinates of rotation.
    cfo, 2015/08/13
       
    """   
    R11 = R[0, 0]; R12 = R[0, 1]; R13 = R[0, 2]
    R21 = R[1, 0]; R22 = R[1, 1]; R23 = R[1, 2]
    R31 = R[2, 0]; R32 = R[2, 1]; R33 = R[2, 2]
    tr = np.trace(R)
    omega = np.empty((3,), dtype=np.float64)
    
    # when trace == -1, i.e., when theta = +-pi, +-3pi, +-5pi, we do something 
    # special
    if(np.abs(tr + 1.0) < 1e-10):
        if(np.abs(R33 + 1.0) > 1e-10):
            omega = (np.pi / np.sqrt(2.0 + 2.0 * R33)) * np.array([R13, R23, 1.0+R33])        
        elif(np.abs(R22 + 1.0) > 1e-10):
            omega = (np.pi / np.sqrt(2.0 + 2.0 * R22)) * np.array([R12, 1.0+R22, R32])
        else:
            omega = (np.pi / np.sqrt(2.0 + 2.0 * R11)) * np.array([1.0+R11, R21, R31])
    else:
        magnitude = 1.0
        tr_3 = tr - 3.0
        if tr_3 < -1e-7:
            theta = np.arccos((tr - 1.0) / 2.0)
            magnitude = theta / (2.0 * np.sin(theta))
        else:
            # when theta near 0, +-2pi, +-4pi, etc. (trace near 3.0)
            # use Taylor expansion: theta \approx 1/2-(t-3)/12 + O((t-3)^2)
            magnitude = 0.5 - tr_3 * tr_3 / 12.0;
            
        omega = magnitude * np.array([R32 - R23, R13 - R31, R21 - R12])
        
    return omega


""" Parse a dataset folder """
def parse_dataset(dataset_dir):
    
     # Parse camera calibration
    cam_file = open('%s/camera.yaml' % dataset_dir)
    cam_data = yaml.safe_load(cam_file)

    image_data = {}

    # Parse image paths       
    lines = [line.rstrip('\n') for line in open('%s/images.txt' % dataset_dir)]
    for line in lines:
        img_id, img_timestamp, img_path = line.split(' ')
        image_data[int(img_id)] = (float(img_timestamp), img_path)
    
     
    # Parse camera trajectory
    lines = [line.rstrip('\n') for line in open('%s/trajectory.txt' % dataset_dir)]
    for line in lines:
        splitted = line.split(' ')
        img_id = int(splitted[0])
        translation = [float(i) for i in splitted[1:4]]
        orientation = [float(i) for i in splitted[4:]]
        image_data[img_id] += (translation + orientation, )
        
    t = [frame[0] for frame in image_data.itervalues()]
    positions = [frame[2][:3] for frame in image_data.itervalues()]
    orientations = [frame[2][-4:] for frame in image_data.itervalues()]
    img_paths = [frame[1] for frame in image_data.itervalues()]
    
    width = cam_data['cam_width']
    height = cam_data['cam_height']
    fx = cam_data['cam_fx']
    fy = cam_data['cam_fy']
    cx = cam_data['cam_cx']
    cy = cam_data['cam_cy']
    
    cam = [width, height, fx, fy, cx, cy]
        
    return t, img_paths, positions, orientations, cam
   
   
class Frame:
    def __init__(self, frame_id, exr_path, use_log=True, blur_size=0, use_scharr=True):
        self.frame_id = frame_id
        self.exr_img = OpenEXR.InputFile(exr_path)
        self.img = extract_grayscale(self.exr_img)
        self.img = Frame.preprocess_image(self.img, use_log=True, blur_size=blur_size)
        self.gradient = compute_gradient(self.img, use_scharr)
        self.z = extract_depth(self.exr_img)
    
    
    @staticmethod
    def preprocess_image(img, use_log=True, blur_size=0):
        if blur_size > 0:
            img = cv2.GaussianBlur(img, (blur_size,blur_size), 0)
            
        if use_log:
            img = safe_log(img)
        return img
        
        

class Trajectory:
    def __init__(self, times, positions, orientations):
        self.t = np.array(times)
        self.pos = np.array(positions)
        self.quat = np.array(orientations)
        _, self.v_b, self.w_b = linear_angular_velocity(self.t, self.pos, self.quat, 1)
    
    
    def T_w_c(self, t):
        closest_id = self.find_closest_id(t)
        T_w_c = matrix_from_quaternion(self.quat[closest_id])
        T_w_c[:3,3] = self.pos[closest_id]
        return T_w_c
        
        
    def find_closest_id(self, t):
        idx = np.searchsorted(self.t, t, side="left")
        if fabs(t - self.t[idx-1]) < fabs(t - self.t[idx]):
            return idx-1
        else:
            return idx   
   
   
   
""" Log with a small offset to avoid problems at zero"""
def safe_log(img):
    eps = 0.001
    return np.log(eps + img)


""" Is pixel (x,y) inside a [width x height] image? (zero-based indexing) """
def is_within(x,y,width,height):
    return (x >= 0 and x < width and y >= 0 and y < height)

  
""" Return normalized vector """
def normalize(v):
    return v / np.linalg.norm(v)
   
   
""" Linear color space to sRGB
    https://en.wikipedia.org/wiki/SRGB#The_forward_transformation_.28CIE_xyY_or_CIE_XYZ_to_sRGB.29 """
def lin2srgb(c):
    a = 0.055
    t = 0.0031308
    c[c <= t] = 12.92 * c[c <= t]
    c[c > t] = (1+a)*np.power(c[c > t], 1.0/2.4) - a
    return c


   

def extract_grayscale(img):
  dw = img.header()['dataWindow']

  size = (dw.max.x - dw.min.x + 1, dw.max.y - dw.min.y + 1)
  precision = Imath.PixelType(Imath.PixelType.FLOAT)
  R = img.channel('R', precision)
  G = img.channel('G', precision)
  B = img.channel('B', precision)
  
  r = np.fromstring(R, dtype = np.float32)
  g = np.fromstring(G, dtype = np.float32)
  b = np.fromstring(B, dtype = np.float32)
  
  r.shape = (size[1], size[0])
  g.shape = (size[1], size[0])
  b.shape = (size[1], size[0])
  
  rgb = cv2.merge([b, g, r])
  grayscale = cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY)

  return grayscale
  


def extract_depth(img):
  dw = img.header()['dataWindow']
  size = (dw.max.x - dw.min.x + 1, dw.max.y - dw.min.y + 1)
  precision = Imath.PixelType(Imath.PixelType.FLOAT)
  Z = img.channel('Z', precision)
  z = np.fromstring(Z, dtype = np.float32)
  z.shape = (size[1], size[0])
  return z


    
    
""" Compute linear and angular velocities along a trajectory """
def linear_angular_velocity(t, positions, orientations, stride):
    w_body = np.zeros((len(t)-stride, 3))
    v_world = np.zeros((len(t)-stride, 3))
    v_body = np.zeros((len(t)-stride, 3))
    for i in range(0, len(t)-stride, stride):
        dt = t[i+stride] - t[i]
        R_wb_t = matrix_from_quaternion(orientations[i])[:3,:3]
        R_wb_t_dt = matrix_from_quaternion(orientations[i+stride])[:3,:3]
        
        v_world[i,:] = 1.0 / dt * (np.array(positions[i+stride])-np.array(positions[i]))
        v_body[i,:] = R_wb_t.transpose().dot(v_world[i,:])
        
        # w(t) = 1/dt * log(R_wb(t)^T * R_wb(t+dt))
        w_body[i,:] = 1.0 / dt * logmap_so3(R_wb_t.transpose().dot(R_wb_t_dt))
        
    return v_world, v_body, w_body
    

""" Compute horizontal and vertical gradients """
def compute_gradient(img, use_scharr=True):
    if use_scharr:
        norm_factor = 32
        gradx = cv2.Scharr(img, cv2.CV_32F, 1, 0, scale=1.0/norm_factor)
        grady = cv2.Scharr(img, cv2.CV_32F, 0, 1, scale=1.0/norm_factor)
    else:
        kx = cv2.getDerivKernels(1, 0, ksize=1, normalize=True)
        ky = cv2.getDerivKernels(0, 1, ksize=1, normalize=True)
        gradx = cv2.sepFilter2D(img, cv2.CV_32F, kx[0], kx[1])
        grady = cv2.sepFilter2D(img, cv2.CV_32F, ky[0], ky[1])
    
    gradient = np.dstack([gradx, grady])
    return gradient
