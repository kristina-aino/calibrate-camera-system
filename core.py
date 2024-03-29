import os
import time
import cv2
import numpy as np
import json
import traceback
from copy import deepcopy

from collections import OrderedDict
from logging import getLogger
from time import sleep
from multiprocessing import Pool
from typing import List, Tuple
from camera_capture_system.datamodel import Camera, ImageParameters
from camera_capture_system.fileIO import CaptureImageSaver
from camera_capture_system.core import MultiCapturePublisher, load_all_cameras_from_config

# --- logging ---

logger = getLogger(__name__)

# --- logging ---


class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return json.JSONEncoder.default(self, obj)

def decode_dict(dct):
    for key, value in dct.items():
        if isinstance(value, list):
            dct[key] = np.array(value)
    return OrderedDict(sorted(list(dct.items()), key=lambda x: x[0]))


def collect_images(cameras_cofig_file: str, num_frames_to_collect: int, frame_transforms: dict[str, str], frame_collection_interval=1):
    
    logger.info("initialize image collection")
    
    # load cameras
    cameras = load_all_cameras_from_config(cameras_cofig_file)
    
    # initialize camera capture publisher
    multi_capture_publisher = MultiCapturePublisher(cameras=cameras, frame_transforms=frame_transforms)
    
    # initialize capture save processor
    image_params = ImageParameters(
        save_path="./images",
        jpg_quality=100,
        png_compression=0,
        output_format="jpg"
    )
    capture_image_saver = CaptureImageSaver(cameras=cameras, image_params=image_params)
    
    try:
        
        logger.info("start image publisher and saver")
        
        # start both processes
        multi_capture_publisher.start()
        capture_image_saver.start()
        
        collected_frames = 0
        while collected_frames < num_frames_to_collect:
            successfull = capture_image_saver.save_image(visualize=True)
            sleep(frame_collection_interval)
            
            if successfull:
                collected_frames += 1
                
            logger.info(f"collected {collected_frames} frames")
    except:
        raise
    finally:
        capture_image_saver.stop()
        multi_capture_publisher.stop()


def check_and_get_target_corners(image_uri: str, num_target_corners: tuple[int, int], subpix_criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.001)) -> Tuple[str, np.ndarray]:
    
    frame = cv2.imread(image_uri)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    ret_corners, corners = cv2.findChessboardCorners(gray, num_target_corners, None)
    
    if not ret_corners:
        return image_uri, None
    return image_uri, cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1), subpix_criteria).squeeze()

def find_all_calibration_targets(cam_uuid: str, num_target_corners: Tuple[int, int], num_workers: int = 8) -> dict[str, Tuple[str, np.ndarray]]:
    
    image_path = os.path.join("images", cam_uuid)
    
    image_uris = [os.path.join(image_path, image_name) for image_name in os.listdir(image_path)]
    process_pool = Pool(num_workers)
    process_results = []
    
    try:
        for image_uri in image_uris:
            result = process_pool.apply_async(check_and_get_target_corners, args=(image_uri, num_target_corners))
            process_results.append(result)
            
        results = OrderedDict([result.get() for result in process_results])
        
        with open(os.path.join("calibration_targets", f"{cam_uuid}.json"), "w") as f:
            json.dump(results, f, cls=NumpyEncoder)
        
    except:
        raise
    finally:
        process_pool.close()
        for result in process_results:
            result.wait()
        process_pool.join()



def Z_norm(array: np.ndarray, axis: int = 0) -> np.ndarray:
    return (array - array.mean(axis=axis)) / array.std(axis=axis)


def remove_images_without_targets(cam_uuid: str):
    
    valid_targets = {}
    
    logger.info(f"{cam_uuid} :: loading extracted target corners and removing images without targets ...")
    with open(os.path.join("calibration_targets", f"{cam_uuid}.json"), "r") as f:
        all_targets = json.load(f, object_hook=decode_dict)
        for k in all_targets:
            if all_targets[k] is None:
                logger.info(f"{cam_uuid} :: {k} has no target, removing ...")
                os.remove(k)
            else:
                valid_targets[k] = all_targets[k]
    
    # save updated list of targets
    with open(os.path.join("calibration_targets", f"{cam_uuid}.json"), "w") as f:
        json.dump(valid_targets, f, cls=NumpyEncoder)

def load_targets(cam_uuid: str) -> dict[str, np.ndarray]:
    with open(os.path.join("calibration_targets", f"{cam_uuid}.json"), "r") as f:
        targets = json.load(f, object_hook=decode_dict)
    return targets

def filter_images_for_intrinsics(targets: dict[str, np.ndarray], point_top_std_exclusion_percentle: float = 10, target_top_inverse_distance_exclusion_percentile: float = 20):
    
    # filter out images where the normaized pattern has too high std
    normalised_patterns = np.stack([Z_norm(valid_target) for valid_target in targets.values()], axis=0)
    per_point_std = np.sqrt((normalised_patterns - normalised_patterns.mean(axis=0))**2)
    top_std_percentile_threshold = np.percentile(per_point_std, (100 - point_top_std_exclusion_percentle), axis=0)
    valid_std_mask = per_point_std <= np.expand_dims(top_std_percentile_threshold, axis=0)
    
    # print("normalised_patterns", normalised_patterns)
    # print("pattern_std", per_point_std)
    # print("top_std_percentile_threshold", top_std_percentile_threshold)
    # print("valid_variance_mask", valid_std_mask)
    
    for i, (k, v) in enumerate(targets.items()):
        if not valid_std_mask[i].all():
            targets[k] = None
    targets = OrderedDict([(k, v) for (k, v) in targets.items() if v is not None])
    
    if len(targets) == 0:
        logger.warning(f"no valid targets after removing high per point std targets")
        return
    logger.info(f"{len(targets)} valid targets after removing high per point std targets")
    
    
    # filter out images with targets means too close to each other
    target_means = np.stack([valid_target.mean(axis=0) for valid_target in targets.values()], axis=0)
    normed_target_means = Z_norm(target_means)
    avg_total_distances_inv = np.stack([np.exp( - ((normed_target_means[i] - normed_target_means)**2).mean(axis=0)).mean(axis=0) for i in range(normed_target_means.shape[0])], axis=0)
    valid_distance_mask = avg_total_distances_inv < np.percentile(avg_total_distances_inv, 100 - target_top_inverse_distance_exclusion_percentile)
    
    # print("normed_target_means", normed_target_means)
    # print("valid_distance_mask", valid_distance_mask)
    # print("targets", target_means[valid_distance_mask])
    # print("avg_total_distance", avg_total_distances_inv)
    
    for i, (k, v) in enumerate(targets.items()):
        if not valid_distance_mask[i]:
            targets[k] = None
    targets = OrderedDict([(k, v) for (k, v) in targets.items() if v is not None])
    
    if len(targets) == 0:
        logger.warning(f"no valid targets after removing high per target mean std targets")
        return
    logger.info(f"{len(targets)} valid targets after removing high per target mean std targets")
    
    return targets

def generate_target_points(num_target_corners: Tuple[int, int], target_size: Tuple[float, float], num_target_points: int):
    model_points = np.mgrid[:num_target_corners[0], :num_target_corners[1]].T.reshape(-1,2)
    model_points = model_points * target_size
    model_points = np.concatenate([model_points, np.zeros([model_points.shape[0], 1])], axis=1).astype(np.float32)
    model_points = np.array(num_target_points * [model_points], dtype=np.float32)
    return model_points

def calibrate_intrinsics(
    cam_uuid: str,
    num_target_corners: Tuple[int, int], # (width, height) number of targets on the checkerboard
    target_size: Tuple[float, float], # (width, height) of each target on the checkerboard in meters (m)
    capture_size: Tuple[int, int], # (width, height) of the capture camera
    batch_size: int = 30,
    optim_iterations: int = 3,
    point_top_std_exclusion_percentle=10,
    target_top_inverse_distance_exclusion_percentile=10):
    
    logger.info(f"calibrating camera {cam_uuid} intrinsics ...")
    
    # load and filter targets
    targets = load_targets(cam_uuid)
    targets = filter_images_for_intrinsics(targets, point_top_std_exclusion_percentle=point_top_std_exclusion_percentle, target_top_inverse_distance_exclusion_percentile=target_top_inverse_distance_exclusion_percentile)
    target_points = np.array([*targets.values()], dtype=np.float32)
    
    # prepare model points
    model_points = generate_target_points(num_target_corners, target_size, target_points.shape[0])
    
    effective_focal_length = capture_size # (width, height) focal length in pixels, because pixel density might be different in height and width and are also unknown
    principle_point = (capture_size[0] / 2, capture_size[1] / 2)    
    
    calibration_matrix = np.array(
        [
            [effective_focal_length[0], 1, principle_point[0]],
            [0, effective_focal_length[1], principle_point[1]],
            [0, 0, 1]
        ]
    )
    dist_coeffs = np.zeros((5, 1), np.float32)
    
    
    logger.info(f"calibrating camera with {batch_size} samples per iteration for {optim_iterations} iterations ...")
    avg_rmse = 0
    avg_calibration_matrix = np.zeros((3, 3), np.float32)
    avg_dist_coeffs = np.zeros((5, 1), np.float32)
    for i in range(optim_iterations):
        
        # select batch
        rand_indexes = np.random.randint(model_points.shape[0], size=batch_size)
        model_points_ = model_points[rand_indexes, :, :]
        target_points_ = target_points[rand_indexes, :, :]
        
        rmse, calibration_matrix_, dist_coeffs_, r_vecs, t_vecs = cv2.calibrateCamera(
            objectPoints=model_points_, 
            imagePoints=target_points_, 
            imageSize=capture_size, 
            cameraMatrix=calibration_matrix, 
            distCoeffs=dist_coeffs)
        
        logger.info(f"iteration {i} :: rmse {rmse}")
        avg_rmse += rmse
        avg_calibration_matrix += calibration_matrix_
        avg_dist_coeffs += dist_coeffs_
    
    logger.info(f"avg_rmse {avg_rmse / optim_iterations}")
    calibration_matrix = avg_calibration_matrix / optim_iterations
    dist_coeffs = avg_dist_coeffs / optim_iterations
    rmse = avg_rmse / optim_iterations
    
    logger.info(f"saving calibration intrinsics to calibration_intrinsics/{cam_uuid}.json")
    with open(os.path.join("calibration_intrinsics", f"{cam_uuid}.json"), "w") as f:
        json.dump({
            "calibration_matrix": calibration_matrix,
            "dist_coeffs": dist_coeffs,
            "avg_rmse": rmse
        }, f, cls=NumpyEncoder)


def calibrate_extrinsics(
    cameras: List[Camera],
    capture_size: Tuple[int, int],
    target_size: Tuple[float, float],
    num_target_corners: Tuple[int, int],
    batch_size: int = 30,
    optim_iterations: int = 3,
    point_top_std_exclusion_percentle=10,
    target_top_inverse_distance_exclusion_percentile=10
):
    
    logger.info(f"calibrating camera extrinsics ...")
    
    # load intrinsics
    intrinsics = {}
    for cam in cameras:
        assert os.path.exists(os.path.join("calibration_intrinsics", f"{cam.uuid}.json")), f"no intrinsics found for camera {cam.uuid}"
        with open(os.path.join("calibration_intrinsics", f"{cam.uuid}.json"), "r") as f:
            intrinsics[cam.uuid] = json.load(f, object_hook=decode_dict)
    
    # load targets and change exclude targets that are not represented in all cameras
    all_frames = {}
    for cam in cameras:
        targets = load_targets(cam.uuid)
        targets = filter_images_for_intrinsics(
            targets, 
            point_top_std_exclusion_percentle=point_top_std_exclusion_percentle, 
            target_top_inverse_distance_exclusion_percentile=target_top_inverse_distance_exclusion_percentile)
        
        for image_uri in targets:
            
            frame_id = os.path.basename(image_uri)
            
            if frame_id not in all_frames:
                all_frames[frame_id] = {cam.uuid: targets[image_uri]}
            else:
                all_frames[frame_id][cam.uuid] = targets[image_uri]
            
    all_valid_frames = {}
    for frame_id in all_frames:
        if len(all_frames[frame_id]) == len(cameras):
            all_valid_frames[frame_id] = all_frames[frame_id]
    
    logger.info(f"found {len(all_valid_frames)} frames with targets in all cameras")
    
    # prepare model points
    model_points = generate_target_points(num_target_corners, target_size, len(all_valid_frames))
    
    all_valid_frames_by_cam = {}
    for cam in cameras:
        all_valid_frames_by_cam[cam.uuid] = np.array([all_valid_frames[frame_id][cam.uuid] for frame_id in all_valid_frames], dtype=np.float32)
    
    
    extrinsic_output = {}
    for main_cam in cameras:
        
        main_cam_matrix = intrinsics[main_cam.uuid]["calibration_matrix"]
        main_cam_dist_coeffs = intrinsics[main_cam.uuid]["dist_coeffs"]
        
        extrinsic_output[main_cam.uuid] = {}
        
        for cam in [c for c in cameras if c.uuid != main_cam.uuid]:
            
            logger.info(f"stereo calibrating {main_cam.uuid} with camera {cam.uuid} stereo with {batch_size} samples per iteration for {optim_iterations} iterations ...")
            avg_rmse = 0
            avg_rotation_mat = np.zeros((3, 3), np.float32)
            avg_translation_vec = np.zeros((3, 1), np.float32)
            avg_essential_mat = np.zeros((3, 3), np.float32)
            avg_fundamental_mat = np.zeros((3, 3), np.float32)
            for i in range(optim_iterations):
                
                # select batch
                rand_indexes = np.random.randint(model_points.shape[0], size=batch_size)
                
                model_points_ = model_points[rand_indexes, :, :]
                main_cam_target_points = all_valid_frames_by_cam[main_cam.uuid][rand_indexes, :, :]
                target2_points = all_valid_frames_by_cam[cam.uuid][rand_indexes, :, :]
                
                rmse, _, _, _, _, r, t, e, f = cv2.stereoCalibrate(
                    objectPoints=model_points_,
                    imagePoints1=main_cam_target_points,
                    imagePoints2=target2_points,
                    imageSize=capture_size,
                    cameraMatrix1=main_cam_matrix,
                    distCoeffs1=main_cam_dist_coeffs,
                    cameraMatrix2=intrinsics[cam.uuid]["calibration_matrix"],
                    distCoeffs2=intrinsics[cam.uuid]["dist_coeffs"],
                    flags=cv2.CALIB_FIX_INTRINSIC
                )
                
                logger.info(f"iteration {i} :: rmse {rmse}")
                avg_rmse += rmse
                avg_rotation_mat += r
                avg_translation_vec += t
                avg_essential_mat += e
                avg_fundamental_mat += f
            
            logger.info(f"avg_rmse {avg_rmse / optim_iterations}")
            rmse = avg_rmse / optim_iterations
            rotation_mat = avg_rotation_mat / optim_iterations
            translation_vec = avg_translation_vec / optim_iterations
            essential_mat = avg_essential_mat / optim_iterations
            fundamental_mat = avg_fundamental_mat / optim_iterations
            
            extrinsic_output[main_cam.uuid][cam.uuid] = {
                "rmse": rmse,
                "rotation_mat": rotation_mat,
                "translation_vec": translation_vec,
                "essential_mat": essential_mat,
                "fundamental_mat": fundamental_mat
            }
        
    with open(os.path.join("calibration_extrinsics.json"), "w") as f:
        json.dump(extrinsic_output, f, cls=NumpyEncoder)


class CameraTransformer:
    def __init__(self, cameras: List[Camera]):
                
        with open("calibration_extrinsics.json", "r") as f:
            self.extrinsics = json.load(f, object_hook=decode_dict)
        
        self.projection_matrices = {}
        for cam in cameras:
            assert os.path.exists(os.path.join("calibration_intrinsics", f"{cam.uuid}.json")), f"no intrinsics found for camera {cam.uuid}"
            with open(os.path.join("calibration_intrinsics", f"{cam.uuid}.json"), "r") as f:
                intrinsics = json.load(f, object_hook=decode_dict)
            
            self.projection_matrices[cam.uuid] = {c: intrinsics["calibration_matrix"] @ np.concatenate([self.extrinsics[cam.uuid][c]["rotation_mat"], self.extrinsics[cam.uuid][c]["translation_vec"]], axis=1) for c in self.extrinsics[cam.uuid]}
            self.projection_matrices[cam.uuid][cam.uuid] = intrinsics["calibration_matrix"] @ np.concatenate([np.eye(3), np.zeros((3, 1))], axis=1)
        
    def triangulate_point(self, point2d: dict[str, np.ndarray], main_cam_uuid: str):
        
        # Create an empty list to store the equations
        equations = []
        
        # For each 2D point
        for cam_uuid, point in point2d.items():
            # Get the corresponding projection matrix
            P = self.projection_matrices[main_cam_uuid][cam_uuid]

            # Create the equations
            equations.append(point[0] * P[2, :] - P[0, :])
            equations.append(point[1] * P[2, :] - P[1, :])

        # Convert the list of equations to a numpy array
        A = np.vstack(equations)

        # Solve the system of equations using SVD
        _, _, V = np.linalg.svd(A)

        # The solution is the last column of V
        point3d = V[-1, :]

        # Convert the solution to homogeneous coordinates
        point3d = point3d / point3d[3]

        return point3d[:3]
    
    def DLT(self, points_2d: dict[str, np.ndarray], main_cam_uuid: str):
        # direct linear transform

        assert len(points_2d) >= 2, "at least points from 2 cameras are required"

        # Get the number of points
        num_points = next(iter(points_2d.values())).shape[0]

        # Initialize an empty list to store the 3D points
        points_3d = []

        # For each point
        for i in range(num_points):
            # Initialize an empty dictionary to store the 2D points for this 3D point
            point2d = {}

            # For each camera
            for cam_uuid, points in points_2d.items():
                # Add the 2D point for this camera to the dictionary
                point2d[cam_uuid] = points[i]

            # Triangulate the 3D point
            point3d = self.triangulate_point(point2d, main_cam_uuid)

            # Add the 3D point to the list
            points_3d.append(point3d)

        return np.vstack(points_3d)