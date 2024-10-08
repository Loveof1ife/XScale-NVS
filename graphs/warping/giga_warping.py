import smtpd
import sys

sys.path.append("../../")
import pdb
import time
import shutil
import logging
import torch
import torch.nn as nn
import torch.nn.functional as F

from PIL import Image, ImageDraw
from tqdm import tqdm
import random
import os
import shutil
import numpy as np
import cv2
import scipy.misc
import math
import matplotlib.pyplot as plt
import time
import trimesh
import nvdiffrast.torch as dr
from configs.parameter import Params
from dataset.data import MVSDataset

class WarpingMachine(nn.Module):
    def __init__(self, params):
        super(WarpingMachine, self).__init__()
        self.params = params
        self.device = self.params.device

        self.cache_root_file_path = self.params.attribute_cache_path
        if not os.path.exists(self.cache_root_file_path):
            os.makedirs(self.cache_root_file_path)

    def forward(self):
        cache_save_folder = os.path.join(self.cache_root_file_path, 'cache_list')
        if not os.path.exists(cache_save_folder):
            os.makedirs(cache_save_folder)
            
        if self.params.splitName is not None:
            vis_save_folder = os.path.join(self.params.datasetFolder, self.params.modelName, self.params.splitName, "normal")
        else:
            vis_save_folder = os.path.join(self.params.datasetFolder, self.params.modelName, "normal")
        os.makedirs(vis_save_folder, exist_ok=True)
        
        with torch.no_grad():
            print('start mesh initialization')

            self.mesh = trimesh.load_mesh(self.params.atlas_load_path)
            self.mesh : trimesh.Trimesh

            self.v = torch.from_numpy(self.mesh.vertices).float().contiguous().to(self.device) #(num_verts, 3) vertex positions
            self.f = torch.from_numpy(self.mesh.faces).int().contiguous().to(self.device)  #Shape: (num_faces, 3) indices of vertices 
            self.vn = torch.tensor(self.mesh.vertex_normals).float().contiguous().to(self.device) # Shape: (num_verts, 3) normal vectors for each vertex

            gs_curvature = trimesh.curvature.discrete_gaussian_curvature_measure(self.mesh) 
            mean_curvature = trimesh.curvature.discrete_mean_curvature_measure(self.mesh)

            self.gs_curvature = torch.from_numpy(gs_curvature).float().contiguous().to(self.device).unsqueeze(1) # Shape: (num_verts, 1)
            self.mean_curvature = torch.from_numpy(mean_curvature).float().contiguous().to(self.device).unsqueeze(1)  # Shape: (num_verts, 1)
            
            self.saliency = self.saliency_computation().unsqueeze(1)# Shape: (num_verts,1)

            coord_max_global = torch.max(self.v, dim=0, keepdims=True)[0]
            coord_min_global = torch.min(self.v, dim=0, keepdims=True)[0]
            center_global = 0.5 * (coord_max_global + coord_min_global)
            # scale_global = (coord_max_global - coord_min_global).max(dim=-1, keepdims=True)[0]
            scale_global = (coord_max_global - coord_min_global + 1e-6)
            self.center = center_global[0, ...]
            self.scale = scale_global[0, ...]
            self.num_verts = self.v.shape[0]
            self.num_facet = self.f.shape[0]
            print('finish mesh initialization')
        
        torch.save(self.num_facet, os.path.join(cache_save_folder, "num_facet.pt"))
        torch.save(self.num_verts, os.path.join(cache_save_folder, "num_verts.pt"))
        torch.save(self.center, os.path.join(cache_save_folder, "center.pt"))
        torch.save(self.scale, os.path.join(cache_save_folder, "scale.pt"))

        
        glctx = dr.RasterizeGLContext()
        # glctx = dr.RasterizeCudaContext()
        
        lits_all = []
        for ras_id in tqdm(range(len(self.params.training_view_list))):
            load_id_list = self.params.training_view_list[ras_id: ras_id + 1]

            self.MVSDataset = MVSDataset(self.params, load_id_list)
            self.cameraPoses = self.MVSDataset.cameraPO4s  # (N_v, 4, 4)
            self.images_ori = self.MVSDataset.imgs_all  # (N_v, H, W, 3)
            self.cameraPositions = self.MVSDataset.cameraTs_new  # (N_v, 3)
            
            print("images: ", len(self.images_ori))
            print("cameraPoses: ", self.cameraPoses.shape)
            print("cameraPositions", self.cameraPositions.shape)
            print("lit: ", self.MVSDataset.lits_all[0])
            # assert len(self.images_ori) == len(self.params.all_view_list), "inconsistent num between params.all_view_list and # images"
            lits_all.append(self.MVSDataset.lits_all[0])
                
            with torch.no_grad():
                for batch_i in range(0, self.cameraPositions.shape[0], 1):
                    mvp = self.cameraPoses[batch_i: batch_i + 1, ...].to(self.device)
                    campos = self.cameraPositions[batch_i: batch_i + 1, ...].to(self.device)
                    images_ori_batch = torch.from_numpy(self.images_ori[batch_i]).type(torch.FloatTensor)
                    num_batch_view = campos.shape[0]
                    
                    H, W, _ = images_ori_batch.shape
                    v_pos_clip = torch.matmul(torch.nn.functional.pad(self.v, pad=(0,1), mode='constant', value=1.0), torch.transpose(mvp, 1, 2))
                    rast, rast_db = dr.rasterize(glctx, v_pos_clip, self.f, (H * self.params.ss_ratio, W * self.params.ss_ratio))  # shape: (N_v, H, W, 4)
                    
                    frg_xyz, _  = dr.interpolate(self.v.unsqueeze(0), rast, self.f)     # shape: (N_v, H, W, 3)
                    frg_normal, _ = dr.interpolate(self.vn.unsqueeze(0), rast, self.f)  # shape: (N_v, H, W, 3)

                    frg_gs_curvature, _ = dr.interpolate(self.gs_curvature.unsqueeze(0), rast, self.f)  # shape: (N_v, H, W, 3)
                    frg_mean_curvature, _ = dr.interpolate(self.mean_curvature.unsqueeze(0), rast, self.f)  # shape: (N_v, H, W, 3)
                    frg_saliency, _ = dr.interpolate(self.sailency.unsqueeze(0), rast, self.f)  # shape: (N_v, H, W, 3)
                
                    frg_normal = F.normalize(frg_normal, p=2, dim=-1, eps=1e-8).contiguous()
                    frg_dir = frg_xyz - campos[:, None, None, :]                     # shape: (N_v, H, W, 3)
                    frg_dir = F.normalize(frg_dir, p=2, dim=-1, eps=1e-8).contiguous()  # shape: (N_v, H, W, 3)
                    inlier_mask = rast[..., 3:] > 0
                    inlier_mask = F.interpolate(inlier_mask.permute(0,3,1,2).float(), scale_factor=1.0 / self.params.ss_ratio, mode='bilinear', align_corners=True).permute(0,2,3,1) > 0.0
                    outlier_mask = ~inlier_mask.detach().cpu()
                    
                    if self.params.undistort_crop_rate_h * self.params.undistort_crop_rate_w > 0:
                        if self.params.undistort_crop_iter is not None and batch_i >= self.params.undistort_crop_iter:
                            temp_h, temp_w = images_ori_batch.shape[0] * self.params.ss_ratio, images_ori_batch.shape[1] * self.params.ss_ratio
                            crop_h = int(temp_h * self.params.undistort_crop_rate_h)
                            crop_w = int(temp_w * self.params.undistort_crop_rate_w)
                            frg_xyz = frg_xyz[:, crop_h: temp_h - crop_h, crop_w: temp_w - crop_w, ...]
                            frg_normal = frg_normal[:, crop_h: temp_h - crop_h, crop_w: temp_w - crop_w, ...]
                            frg_dir = frg_dir[:, crop_h: temp_h - crop_h, crop_w: temp_w - crop_w, ...]
                            temp_h, temp_w = images_ori_batch.shape[0], images_ori_batch.shape[1]
                            crop_h = int(temp_h * self.params.undistort_crop_rate_h)
                            crop_w = int(temp_w * self.params.undistort_crop_rate_w)
                            images_ori_batch = images_ori_batch[crop_h: temp_h - crop_h, crop_w: temp_w - crop_w, ...]
                            inlier_mask = inlier_mask[:, crop_h: temp_h - crop_h, crop_w: temp_w - crop_w, ...]

                    for v in range(num_batch_view):
                        colour = images_ori_batch[inlier_mask[v, ..., 0].detach().cpu()].detach().cpu()
                        xyz = []
                        normal = []
                        viewdr = []
                        for i in range(self.params.ss_ratio):
                            for j in range(self.params.ss_ratio):
                                tmp_xyz = frg_xyz[v][i::self.params.ss_ratio, j::self.params.ss_ratio, ...]
                                tmp_nrm = frg_normal[v][i::self.params.ss_ratio, j::self.params.ss_ratio, ...]
                                tmp_vdr = frg_dir[v][i::self.params.ss_ratio, j::self.params.ss_ratio, ...]
                                xyz.append(tmp_xyz)
                                normal.append(tmp_nrm)
                                viewdr.append(tmp_vdr)
                        
                        nrm_vis = normal[0].detach().cpu()
                        xyz = torch.stack(xyz, dim=0)[:, inlier_mask[v, ..., 0].detach().cpu()].detach().cpu().permute(1,0,2)
                        normal = torch.stack(normal, dim=0)[:, inlier_mask[v, ..., 0].detach().cpu()].detach().cpu().permute(1,0,2)
                        viewdr = torch.stack(viewdr, dim=0)[:, inlier_mask[v, ..., 0].detach().cpu()].detach().cpu().permute(1,0,2)
                        
                        nrm_vis[~inlier_mask[v, ..., 0].detach().cpu()] == 0.0
                        vis_n = (nrm_vis.numpy()[..., ::-1] * 0.5 + 0.5)*255
                        # images_ori_batch[~frg.mask_ori[v, ..., 0]] = 0.0
                        vis_c = images_ori_batch.cpu().numpy()*255
                        # vis_c = F.interpolate(images_ori_batch[None, ...].permute(0, 3, 1, 2), scale_factor=self.params.ss_ratio, mode='bilinear', align_corners=True)[0].permute(1,2,0).cpu().numpy()*255
                        vis = vis_n * 0.3 + vis_c * 0.7
                        # vis = vis_n
                        cv2.imwrite(os.path.join(vis_save_folder, "nrm_{}_{}.jpg".format(self.MVSDataset.item_list[0], ras_id)), vis[..., ::-1])
                        
                        checker = (normal * -viewdr).sum(dim=-1) > 0.
                        print(checker.sum(), checker.shape[0] * checker.shape[1])

                        torch.save(xyz, os.path.join(cache_save_folder, "xyz_{}.pt".format(self.params.training_view_list[ras_id])))
                        torch.save(normal, os.path.join(cache_save_folder, "normal_{}.pt".format(self.params.training_view_list[ras_id])))
                        torch.save(colour, os.path.join(cache_save_folder, "colour_{}.pt".format(self.params.training_view_list[ras_id])))
                        torch.save(viewdr, os.path.join(cache_save_folder, "viewdr_{}.pt".format(self.params.training_view_list[ras_id])))
                        
        torch.save(lits_all, os.path.join(cache_save_folder, "lits_all.pt"))

    def saliency_computation(self):
         # Step 1: Create adjacent set using the mesh edges
        edges = self.mesh.edges_unique
        adjacent_set = {i: set() for i in range(self.num_verts) }

        saliency = []

        for edge in edges:
            v0, v1 = edge
            adjacent_set[v0].add(v1)
            adjacent_set[v1].add(v0)

        for i in range(self.num_verts):
  
            neighbors_1 = adjacent_set[i]
            
            neighbors_2 = set()
            for neighbor in neighbors_1:
                neighbors_2.update(adjacent_set)
            
            adjacent_indices_1 = list(adjacent_set)
            adjacent_curvatures_1= self.mean_curvature[adjacent_indices_1]
    
            # self.v: This is a tensor containing the vertex positions of your mesh. 
            # Suppose it has a shape (num_verts, 3), where num_verts is the number of vertices in your mesh, and each vertex has three coordinates (x, y, z).
            # self.v[all_neighbors]: In PyTorch (and in NumPy), you can index a tensor with a list or tensor of indices. 
            # Here, self.v[djacent_indices_x] extracts the vertex positions of all the vertices that are listed in all_neighbors. The result is a tensor of shape (num_neighbors, 3) where num_neighbors is the total number of 1-neighbors and 2-neighbors.
            # self.v[i]: This extracts the position of the vertex i itself. The shape of this tensor is (3,).
            #Because self.v[i] has a shape of (3,), broadcasting occurs in PyTorch, automatically expanding self.v[i] to shape (num_neighbors, 3) to match the shape of self.v[all_neighbors]. 

            distances_1 = torch.norm(self.v[adjacent_indices_1] - self.v[i], dim = 1)
            gaussian_weights_1 = torch.exp(-distances_1**2 / (2.0 * self.params.gaussian_sigma**2) )
            weighted_avg_curvature_1 = torch.sum(gaussian_weights_1 * adjacent_curvatures_1)  / torch.sum(gaussian_weights_1)

            neighbors_12 = neighbors_1.union(neighbors_2)
            adjacent_indices_12 = list(neighbors_12) 
            adjacent_curvatures_12= self.mean_curvature[adjacent_indices_12]
            distances_12 = torch.norm(self.v[neighbors_12] - self.v[i], dim=1)
            gaussian_weights_12 = torch.exp(-distances_12**2 / (2.0 * self.params.gaussian_sigma**2) )
            weighted_avg_curvature_12 = torch.sum(gaussian_weights_12 * adjacent_curvatures_12)  / torch.sum(gaussian_weights_12)

            filter12 = gaussian_weights_12 - gaussian_weights_1

            # saliency: a list where each element is a tensor of shape (1,).[tensor([0.1234]), tensor([0.2345]), ..., tensor([0.3456])] with 1000 elements, each of shape (1,).
            saliency.append(filter12)

        # A single tensor of shape (num_verts,).
        saliency = torch.stack(saliency, dim=0).to(self.device)
        return saliency

if __name__ == "__main__":
    params = Params()
    warping_machine = WarpingMachine(params=params)
    warping_machine()