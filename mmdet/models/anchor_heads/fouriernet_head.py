import torch
import torch.nn as nn
from mmcv.cnn import normal_init
from torch import Tensor
import numpy as np

from mmdet.core import distance2bbox, force_fp32, multi_apply, multiclass_nms_with_mask
from mmdet.ops import ModulatedDeformConvPack

from ..builder import build_loss
from ..registry import HEADS
from ..utils import ConvModule, Scale, bias_init_with_prob, build_norm_layer
import math

INF = 1e8


def get_mask_sample_region(gt_bb, mask_center, strides, num_points_per, gt_xs, gt_ys, radius=1.0):
    # This function checks if a feature pixel is near the center of an instance
    # returns true or false for every pixel and object size 204600 * 8
    center_y = mask_center[..., 0]
    center_x = mask_center[..., 1]
    center_gt = gt_bb.new_zeros(gt_bb.shape)
    # no gt
    if center_x[..., 0].sum() == 0:
        return gt_xs.new_zeros(gt_xs.shape, dtype=torch.uint8)

    beg = 0
    for level, n_p in enumerate(num_points_per):
        end = beg + n_p  # setting where to stop for each head
        stride = strides[level] * radius
        xmin = center_x[beg:end] - stride
        ymin = center_y[beg:end] - stride
        xmax = center_x[beg:end] + stride
        ymax = center_y[beg:end] + stride
        # limit sample region in gt
        center_gt[beg:end, :, 0] = torch.where(xmin > gt_bb[beg:end, :, 0], xmin, gt_bb[beg:end, :, 0])
        center_gt[beg:end, :, 1] = torch.where(ymin > gt_bb[beg:end, :, 1], ymin, gt_bb[beg:end, :, 1])
        center_gt[beg:end, :, 2] = torch.where(xmax > gt_bb[beg:end, :, 2], gt_bb[beg:end, :, 2], xmax)
        center_gt[beg:end, :, 3] = torch.where(ymax > gt_bb[beg:end, :, 3], gt_bb[beg:end, :, 3], ymax)
        beg = end

    left = gt_xs - center_gt[..., 0]
    right = center_gt[..., 2] - gt_xs
    top = gt_ys - center_gt[..., 1]
    bottom = center_gt[..., 3] - gt_ys
    center_bbox = torch.stack((left, top, right, bottom), -1)
    inside_gt_bbox_mask = center_bbox.min(-1)[0] > 0  # 上下左右都>0 就是在bbox里面
    return inside_gt_bbox_mask


def get_polar_coordinates(c_x, c_y, pos_mask_contour, n=72):
    if len(pos_mask_contour.shape) == 2:
        ct = pos_mask_contour
    else:
        ct = pos_mask_contour[:, 0, :]
    x = ct[:, 0] - c_x
    y = ct[:, 1] - c_y
    angle = torch.atan2(x, y) * 180 / np.pi
    angle[angle < 0] += 360
    angle = angle.int()
    dist = torch.sqrt(x ** 2 + y ** 2)
    angle, idx = torch.sort(angle)
    dist = dist[idx]

    interval = 360 // n
    new_coordinate = {}
    for i in range(0, 360, interval):
        if i in angle:
            d = dist[angle == i].max()
            new_coordinate[i] = d
        elif i + 1 in angle:
            d = dist[angle == i + 1].max()
            new_coordinate[i] = d
        elif i - 1 in angle:
            d = dist[angle == i - 1].max()
            new_coordinate[i] = d
        elif i + 2 in angle:
            d = dist[angle == i + 2].max()
            new_coordinate[i] = d
        elif i - 2 in angle:
            d = dist[angle == i - 2].max()
            new_coordinate[i] = d
        elif i + 3 in angle:
            d = dist[angle == i + 3].max()
            new_coordinate[i] = d
        elif i - 3 in angle:
            d = dist[angle == i - 3].max()
            new_coordinate[i] = d

    distances = torch.zeros(n)

    for a in range(0, 360, interval):
        if a in new_coordinate.keys():
            distances[a // interval] = new_coordinate[a]
        else:
            new_coordinate[a] = torch.tensor(1e-6)
            distances[a // interval] = 1e-6

    return distances, new_coordinate


def polar_centerness_target(pos_mask_targets, max_centerness=None):
    # only calculate pos centerness targets, otherwise there may be nan
    centerness_targets = torch.sqrt(pos_mask_targets.min() / pos_mask_targets.max())
    if max_centerness:
        centerness_targets /= max_centerness
    return centerness_targets.clamp_max(1.0)


def get_points_single(featmap_size, stride, dtype, device):
    h, w = featmap_size
    x_range = torch.arange(
        0, w * stride, stride, dtype=dtype, device=device)
    y_range = torch.arange(
        0, h * stride, stride, dtype=dtype, device=device)
    y, x = torch.meshgrid(y_range, x_range)
    points = torch.stack(
        (x.reshape(-1), y.reshape(-1)), dim=-1) + stride // 2
    return points


@HEADS.register_module
class FourierNetHead(nn.Module):

    def __init__(self,
                 num_classes,
                 in_channels,
                 feat_channels=256,
                 stacked_convs=4,
                 strides=(4, 8, 16, 32, 64),
                 regress_ranges=((-1, 64), (64, 128), (128, 256), (256, 512), (512, INF)),
                 use_dcn=False,
                 mask_nms=False,
                 bbox_from_mask=False,
                 center_sample=True,
                 use_mask_center=True,
                 radius=1.5,
                 loss_cls=None,
                 loss_bbox=None,
                 loss_mask=None,
                 loss_on_coe=False,
                 loss_centerness=None,
                 conv_cfg=None,
                 norm_cfg=None,
                 contour_points=360,
                 use_fourier=False,
                 num_coe=36,
                 visulize_coe=36,
                 centerness_factor=0.5,
                 normalized_centerness=False):
        super(FourierNetHead, self).__init__()
        self.use_fourier = use_fourier
        self.contour_points = contour_points
        self.num_coe = num_coe
        self.visulize_coe = visulize_coe
        self.interval = 360 // self.contour_points
        self.num_classes = num_classes
        self.cls_out_channels = num_classes - 1
        self.in_channels = in_channels
        self.feat_channels = feat_channels
        self.stacked_convs = stacked_convs
        self.strides = strides
        self.regress_ranges = regress_ranges
        self.loss_cls = build_loss(loss_cls)
        self.loss_bbox = build_loss(loss_bbox)
        self.loss_mask = build_loss(loss_mask)
        self.loss_on_coe = loss_on_coe
        self.loss_centerness = build_loss(loss_centerness)
        self.conv_cfg = conv_cfg
        self.norm_cfg = norm_cfg
        self.fp16_enabled = False
        self.use_dcn = use_dcn
        self.mask_nms = mask_nms
        self.bbox_from_mask = bbox_from_mask
        self.vis_num = 1000
        self.count = 0
        self.center_sample = center_sample
        self.use_mask_center = use_mask_center
        self.radius = radius
        self.centerness_factor = centerness_factor
        self.normalized_centerness = normalized_centerness
        self._init_layers()

    def _init_layers(self):
        self.cls_convs = nn.ModuleList()
        if not self.bbox_from_mask:
            self.reg_convs = nn.ModuleList()
        self.mask_convs = nn.ModuleList()
        for i in range(self.stacked_convs):
            chn = self.in_channels if i == 0 else self.feat_channels
            if not self.use_dcn:
                self.cls_convs.append(
                    ConvModule(
                        chn,
                        self.feat_channels,
                        3,
                        stride=1,
                        padding=1,
                        conv_cfg=self.conv_cfg,
                        norm_cfg=self.norm_cfg,
                        bias=self.norm_cfg is None))
                if not self.bbox_from_mask:
                    self.reg_convs.append(
                        ConvModule(
                            chn,
                            self.feat_channels,
                            3,
                            stride=1,
                            padding=1,
                            conv_cfg=self.conv_cfg,
                            norm_cfg=self.norm_cfg,
                            bias=self.norm_cfg is None))
                self.mask_convs.append(
                    ConvModule(
                        chn,
                        self.feat_channels,
                        3,
                        stride=1,
                        padding=1,
                        conv_cfg=self.conv_cfg,
                        norm_cfg=self.norm_cfg,
                        bias=self.norm_cfg is None))
            else:
                self.cls_convs.append(
                    ModulatedDeformConvPack(
                        chn,
                        self.feat_channels,
                        3,
                        stride=1,
                        padding=1,
                        dilation=1,
                        deformable_groups=1,
                    ))
                if self.norm_cfg:
                    self.cls_convs.append(build_norm_layer(self.norm_cfg, self.feat_channels)[1])
                self.cls_convs.append(nn.ReLU(inplace=True))

                if not self.bbox_from_mask:
                    self.reg_convs.append(
                        ModulatedDeformConvPack(
                            chn,
                            self.feat_channels,
                            3,
                            stride=1,
                            padding=1,
                            dilation=1,
                            deformable_groups=1,
                        ))
                    if self.norm_cfg:
                        self.reg_convs.append(build_norm_layer(self.norm_cfg, self.feat_channels)[1])
                    self.reg_convs.append(nn.ReLU(inplace=True))

                self.mask_convs.append(
                    ModulatedDeformConvPack(
                        chn,
                        self.feat_channels,
                        3,
                        stride=1,
                        padding=1,
                        dilation=1,
                        deformable_groups=1,
                    ))
                if self.norm_cfg:
                    self.mask_convs.append(build_norm_layer(self.norm_cfg, self.feat_channels)[1])
                self.mask_convs.append(nn.ReLU(inplace=True))

        self.polar_cls = nn.Conv2d(
            self.feat_channels, self.cls_out_channels, 3, padding=1)
        self.polar_reg = nn.Conv2d(self.feat_channels, 4, 3, padding=1)
        if self.use_fourier:
            self.polar_mask = nn.Conv2d(self.feat_channels, self.num_coe * 2, 3, padding=1)
        else:
            self.polar_mask = nn.Conv2d(self.feat_channels, self.contour_points, 3, padding=1)
        self.polar_centerness = nn.Conv2d(self.feat_channels, 1, 3, padding=1)

        self.scales_bbox = nn.ModuleList([Scale(1.0) for _ in self.strides])
        self.scales_mask = nn.ModuleList([Scale(1.0) for _ in self.strides])

    def init_weights(self):
        if not self.use_dcn:
            for m in self.cls_convs:
                normal_init(m.conv, std=0.01)
            if not self.bbox_from_mask:
                for m in self.reg_convs:
                    normal_init(m.conv, std=0.01)
            for m in self.mask_convs:
                normal_init(m.conv, std=0.01)
        else:
            pass

        bias_cls = bias_init_with_prob(0.01)
        normal_init(self.polar_cls, std=0.01, bias=bias_cls)
        normal_init(self.polar_reg, std=0.01)
        normal_init(self.polar_mask, std=0.01)
        normal_init(self.polar_centerness, std=0.01)

    def forward(self, feats):
        return multi_apply(self.forward_single, feats, self.scales_bbox, self.scales_mask)

    def forward_single(self, x, scale_bbox, scale_mask):
        cls_feat = x
        reg_feat = x
        mask_feat = x

        for cls_layer in self.cls_convs:
            cls_feat = cls_layer(cls_feat)
        cls_score = self.polar_cls(cls_feat)

        for mask_layer in self.mask_convs:
            mask_feat = mask_layer(mask_feat)
        if self.use_fourier:
            mask_pred = self.polar_mask(mask_feat)
            mask_pred = scale_mask(mask_pred)
        else:
            mask_pred = scale_mask(self.polar_mask(mask_feat)).float().exp()

        centerness = self.polar_centerness(cls_feat)

        if not self.bbox_from_mask:
            for reg_layer in self.reg_convs:
                reg_feat = reg_layer(reg_feat)
            # scale the bbox_pred of different level
            # float to avoid overflow when enabling FP16
            bbox_pred = scale_bbox(self.polar_reg(reg_feat)).float().exp()
        else:
            bbox_pred = mask_pred[:, :4, :, :]

        return cls_score, bbox_pred, centerness, mask_pred

    @force_fp32(apply_to=('cls_scores', 'bbox_preds', 'mask_preds', 'centernesses'))
    def loss(self,
             cls_scores,
             bbox_preds,
             centernesses,
             mask_preds,
             gt_bboxes,
             gt_labels,
             img_metas,
             cfg,
             gt_masks=None,
             gt_bboxes_ignore=None,
             gt_centers=None,
             gt_max_centerness=None):
        assert len(cls_scores) == len(bbox_preds) == len(centernesses) == len(mask_preds)
        featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
        all_level_points = self.get_points(featmap_sizes, bbox_preds[0].dtype,
                                           bbox_preds[0].device)
        self.num_points_per_level = [i.size()[0] for i in all_level_points]

        labels, bbox_targets, mask_targets, centerness_targets = self.polar_target(all_level_points, gt_labels,
                                                                                   gt_bboxes, gt_masks, gt_centers,
                                                                                   gt_max_centerness)

        num_imgs = cls_scores[0].size(0)
        # flatten cls_scores, bbox_preds and centerness
        flatten_cls_scores = [
            cls_score.permute(0, 2, 3, 1).reshape(-1, self.cls_out_channels)
            for cls_score in cls_scores]

        flatten_centerness = [
            centerness.permute(0, 2, 3, 1).reshape(-1)
            for centerness in centernesses
        ]
        if self.use_fourier:
            if self.loss_on_coe:
                flatten_mask_preds = [
                    mask_pred.permute(0, 2, 3, 1).reshape(-1, self.num_coe, 2)
                    for mask_pred in mask_preds
                ]
            else:
                flatten_mask_preds = []
                flatten_bbox_preds = []
                for mask_pred, points in zip(mask_preds, all_level_points):
                    mask_pred = mask_pred.permute(0, 2, 3, 1).reshape(-1, self.num_coe, 2)
                    if self.bbox_from_mask:
                        xy, m = self.distance2mask(points.repeat(num_imgs, 1), mask_pred, train=True)
                        b = torch.stack([xy[:, 0].min(1)[0],
                                         xy[:, 1].min(1)[0],
                                         xy[:, 0].max(1)[0],
                                         xy[:, 1].max(1)[0]], -1)
                        flatten_bbox_preds.append(b)
                        flatten_mask_preds.append(m)
                    else:
                        m = torch.irfft(torch.cat([mask_pred, torch.zeros(mask_pred.shape[0],
                                                                          self.contour_points - self.num_coe, 2).to(
                            "cuda")], 1), 1, True, False).float().exp()
                        flatten_mask_preds.append(m)

        else:
            flatten_mask_preds = [
                mask_pred.permute(0, 2, 3, 1).reshape(-1, self.contour_points)
                for mask_pred in mask_preds
            ]
        if not self.bbox_from_mask:
            flatten_bbox_preds = [
                bbox_pred.permute(0, 2, 3, 1).reshape(-1, 4)
                for bbox_pred in bbox_preds
            ]

        flatten_cls_scores = torch.cat(flatten_cls_scores)  # [num_pixel, 80]
        flatten_bbox_preds = torch.cat(flatten_bbox_preds)  # [num_pixel, 4]
        flatten_mask_preds = torch.cat(flatten_mask_preds)  # [num_pixel, n]
        flatten_centerness = torch.cat(flatten_centerness)  # [num_pixel]

        flatten_labels = torch.cat(labels).long()  # [num_pixel]
        flatten_centerness_targets = torch.cat(centerness_targets)
        flatten_bbox_targets = torch.cat(bbox_targets)  # [num_pixel, 4]
        flatten_mask_targets = torch.cat(mask_targets)  # [num_pixel, n]
        flatten_points = torch.cat([points.repeat(num_imgs, 1)
                                    for points in all_level_points])  # [num_pixel,2]
        pos_inds = flatten_labels.nonzero().reshape(-1)
        num_pos = len(pos_inds)

        loss_cls = self.loss_cls(flatten_cls_scores, flatten_labels,
                                 avg_factor=num_pos + num_imgs)  # avoid num_pos is 0
        pos_bbox_preds = flatten_bbox_preds[pos_inds]
        pos_centerness = flatten_centerness[pos_inds]
        pos_mask_preds = flatten_mask_preds[pos_inds]

        if num_pos > 0:
            pos_bbox_targets = flatten_bbox_targets[pos_inds]
            pos_mask_targets = flatten_mask_targets[pos_inds]
            pos_centerness_targets = flatten_centerness_targets[pos_inds]

            pos_points = flatten_points[pos_inds]
            if self.bbox_from_mask:
                pos_decoded_bbox_preds = pos_bbox_preds
            else:
                pos_decoded_bbox_preds = distance2bbox(pos_points, pos_bbox_preds)
            pos_decoded_target_preds = distance2bbox(pos_points,
                                                     pos_bbox_targets)

            # centerness weighted iou loss
            loss_bbox = self.loss_bbox(
                pos_decoded_bbox_preds,
                pos_decoded_target_preds,
                weight=pos_centerness_targets,
                avg_factor=pos_centerness_targets.sum())

            if self.loss_on_coe:
                pos_mask_targets = torch.rfft(pos_mask_targets, 1, True, False)
                pos_mask_targets = pos_mask_targets[..., :self.num_coe, :]
                loss_mask = self.loss_mask(pos_mask_preds,
                                           pos_mask_targets)
            else:
                loss_mask = self.loss_mask(pos_mask_preds,
                                           pos_mask_targets,
                                           weight=pos_centerness_targets,
                                           avg_factor=pos_centerness_targets.sum()
                                           )

            loss_centerness = self.loss_centerness(pos_centerness,
                                                   pos_centerness_targets)
        else:
            loss_bbox = pos_bbox_preds.sum()
            loss_mask = pos_mask_preds.sum()
            loss_centerness = pos_centerness.sum()

        return dict(
            loss_cls=loss_cls,
            loss_bbox=loss_bbox,
            loss_mask=loss_mask,
            loss_centerness=loss_centerness)

    def get_points(self, featmap_sizes, dtype, device):
        """Get points according to feature map sizes.

        Args:
            featmap_sizes (list[tuple]): Multi-level feature map sizes.
            dtype (torch.dtype): Type of points.
            device (torch.device): Device of points.

        Returns:
            tuple: points of each image.
        """
        mlvl_points = []
        for i in range(len(featmap_sizes)):
            mlvl_points.append(
                get_points_single(featmap_sizes[i], self.strides[i],
                                  dtype, device))
        return mlvl_points

    def polar_target(self, points, labels_list, bbox_list, mask_list, centers_list, centerness_list):
        assert len(points) == len(self.regress_ranges)
        num_levels = len(points)
        # expand regress ranges to align with points
        expanded_regress_ranges = [
            points[i].new_tensor(self.regress_ranges[i])[None].expand_as(
                points[i]) for i in range(num_levels)
        ]
        # concat all levels points and regress ranges
        concat_regress_ranges = torch.cat(expanded_regress_ranges, dim=0)
        concat_points = torch.cat(points, dim=0)
        # get labels and bbox_targets of each image
        labels_list, bbox_targets_list, mask_targets_list, centerness_targets_list = multi_apply(
            self.polar_target_single,
            bbox_list,
            mask_list,
            labels_list,
            centers_list,
            centerness_list,
            points=concat_points,
            regress_ranges=concat_regress_ranges)

        # split to per img, per level
        num_points = [center.size(0) for center in points]
        labels_list = [labels.split(num_points, 0) for labels in labels_list]
        centerness_targets_list = [
            centerness_targets.split(num_points, 0)
            for centerness_targets in centerness_targets_list
        ]
        bbox_targets_list = [
            bbox_targets.split(num_points, 0)
            for bbox_targets in bbox_targets_list
        ]
        mask_targets_list = [
            mask_targets.split(num_points, 0)
            for mask_targets in mask_targets_list
        ]

        # concat per level image
        concat_lvl_labels = []
        concat_lvl_centerness_targets = []
        concat_lvl_bbox_targets = []
        concat_lvl_mask_targets = []
        for i in range(num_levels):
            concat_lvl_labels.append(
                torch.cat([labels[i] for labels in labels_list]))
            concat_lvl_centerness_targets.append(
                torch.cat([centerness[i] for centerness in centerness_targets_list]))
            concat_lvl_bbox_targets.append(
                torch.cat(
                    [bbox_targets[i] for bbox_targets in bbox_targets_list]))
            concat_lvl_mask_targets.append(
                torch.cat(
                    [mask_targets[i] for mask_targets in mask_targets_list]))

        return concat_lvl_labels, concat_lvl_bbox_targets, concat_lvl_mask_targets, concat_lvl_centerness_targets

    def polar_target_single(self, gt_bboxes, gt_masks, gt_labels, mask_centers, gt_max_centerness, points,
                            regress_ranges):

        # Sum of all points ever
        num_points = points.size(0)
        # Number of ground truth objects
        num_gts = gt_labels.size(0)
        if num_gts == 0:
            return gt_labels.new_zeros(num_points), \
                   gt_bboxes.new_zeros((num_points, 4))

        # Area of all bounding boxes
        areas = (gt_bboxes[:, 2] - gt_bboxes[:, 0] + 1) * \
                (gt_bboxes[:, 3] - gt_bboxes[:, 1] + 1)

        areas = areas[None].repeat(num_points, 1)  # Make a copy for all points

        # Make a copy for each object (adds a dimension equal to num of ground truth bboxes)
        regress_ranges = regress_ranges[:, None, :].expand(num_points, num_gts, 2)
        gt_bboxes = gt_bboxes[None].expand(num_points, num_gts, 4)  # Make a copy for all points
        xs, ys = points[:, 0], points[:, 1]
        xs = xs[:, None].expand(num_points, num_gts)  # Make a copy for each object
        ys = ys[:, None].expand(num_points, num_gts)  # Make a copy for each object

        # The pixel distance between all object bounding boxes and all points in feature map
        left = xs - gt_bboxes[..., 0]
        right = gt_bboxes[..., 2] - xs
        top = ys - gt_bboxes[..., 1]
        bottom = gt_bboxes[..., 3] - ys
        bbox_targets = torch.stack((left, top, right, bottom), -1)

        # make centerness regression targets
        mask_centers = mask_centers[None].expand(num_points, num_gts, 2)
        if self.center_sample:
            if self.use_mask_center:
                inside_gt_bbox_mask = get_mask_sample_region(gt_bboxes,
                                                             mask_centers,
                                                             self.strides,
                                                             self.num_points_per_level,
                                                             xs,
                                                             ys,
                                                             radius=self.radius)
            else:
                inside_gt_bbox_mask = self.get_sample_region(gt_bboxes,
                                                             self.strides,
                                                             self.num_points_per_level,
                                                             xs,
                                                             ys,
                                                             radius=self.radius)
        else:
            inside_gt_bbox_mask = bbox_targets.min(-1)[0] > 0

        # condition2: limit the regression range for each location
        # returns the maximum vector in the bounding box targets
        max_regress_distance = bbox_targets.max(-1)[0]

        # check if it is in regress range
        inside_regress_range = (max_regress_distance >= regress_ranges[..., 0]) & \
                               (max_regress_distance <= regress_ranges[..., 1])

        areas[inside_gt_bbox_mask == 0] = INF
        areas[inside_regress_range == 0] = INF
        min_area, min_area_inds = areas.min(dim=1)

        # set the ground truth labels
        labels = gt_labels[min_area_inds]
        labels[min_area == INF] = 0
        bbox_targets = bbox_targets[range(num_points), min_area_inds]

        # get the indexes of features which have objects
        pos_inds = labels.nonzero().reshape(-1)
        mask_targets = torch.zeros(num_points, self.contour_points, device=bbox_targets.device).float()
        centerness_target = torch.zeros(num_points, device=bbox_targets.device).float()
        pos_mask_ids = min_area_inds[pos_inds]

        for p, i in zip(pos_inds, pos_mask_ids):
            x, y = points[p]
            pos_mask_contour = gt_masks[i]
            dists, _ = get_polar_coordinates(x, y, pos_mask_contour, self.contour_points)
            mask_targets[p] = dists
            if self.normalized_centerness:
                centerness_target[p] = polar_centerness_target(dists, gt_max_centerness[i])
            else:
                centerness_target[p] = polar_centerness_target(dists)
        return labels, bbox_targets, mask_targets, centerness_target

    @force_fp32(apply_to=('cls_scores', 'bbox_preds', 'centernesses'))
    def get_bboxes(self,
                   cls_scores,
                   bbox_preds,
                   centernesses,
                   mask_preds,
                   img_metas,
                   cfg,
                   rescale=None):
        assert len(cls_scores) == len(bbox_preds)
        num_levels = len(cls_scores)

        featmap_sizes = [featmap.size()[-2:] for featmap in cls_scores]
        mlvl_points = self.get_points(featmap_sizes, bbox_preds[0].dtype, bbox_preds[0].device)
        result_list = []
        for img_id in range(len(img_metas)):
            cls_score_list = [
                cls_scores[i][img_id].detach() for i in range(num_levels)
            ]
            bbox_pred_list = [
                bbox_preds[i][img_id].detach() for i in range(num_levels)
            ]
            centerness_pred_list = [
                centernesses[i][img_id].detach() for i in range(num_levels)
            ]
            mask_pred_list = [
                mask_preds[i][img_id].detach() for i in range(num_levels)
            ]
            img_shape = img_metas[img_id]['img_shape']
            scale_factor = img_metas[img_id]['scale_factor']
            det_bboxes = self.get_bboxes_single(cls_score_list,
                                                bbox_pred_list,
                                                mask_pred_list,
                                                centerness_pred_list,
                                                mlvl_points, img_shape,
                                                scale_factor, cfg, rescale)
            result_list.append(det_bboxes)
        return result_list

    def get_bboxes_single(self,
                          cls_scores,
                          bbox_preds,
                          mask_preds,
                          centernesses,
                          mlvl_points,
                          img_shape,
                          scale_factor,
                          cfg,
                          rescale=False):
        assert len(cls_scores) == len(bbox_preds) == len(mlvl_points)
        mlvl_bboxes = []
        mlvl_scores = []
        mlvl_masks = []
        mlvl_centerness = []
        for cls_score, bbox_pred, mask_pred, centerness, points in zip(
                cls_scores, bbox_preds, mask_preds, centernesses, mlvl_points):
            assert cls_score.size()[-2:] == bbox_pred.size()[-2:]
            scores = cls_score.permute(1, 2, 0).reshape(
                -1, self.cls_out_channels).sigmoid()

            centerness = centerness.permute(1, 2, 0).reshape(-1).sigmoid()
            bbox_pred = bbox_pred.permute(1, 2, 0).reshape(-1, 4)
            if self.use_fourier:
                mask_pred = mask_pred.permute(1, 2, 0).reshape(-1, self.num_coe * 2)
            else:
                mask_pred = mask_pred.permute(1, 2, 0).reshape(-1, self.contour_points)
            nms_pre = cfg.get('nms_pre', -1)
            if 0 < nms_pre < scores.shape[0]:
                max_scores, _ = (scores * centerness[:, None]).max(dim=1)
                _, topk_inds = max_scores.topk(nms_pre)
                points = points[topk_inds, :]
                bbox_pred = bbox_pred[topk_inds, :]
                mask_pred = mask_pred[topk_inds, :]
                scores = scores[topk_inds, :]
                centerness = centerness[topk_inds]
            if not self.bbox_from_mask:
                bboxes = distance2bbox(points, bbox_pred, max_shape=img_shape)
                # masks, _ = self.distance2mask(points, mask_pred, bbox=bboxes)
                masks, _ = self.distance2mask(points, mask_pred, max_shape=img_shape)
            else:
                masks, _ = self.distance2mask(points, mask_pred, max_shape=img_shape)
                bboxes = torch.stack([masks[:, 0].min(1)[0],
                                      masks[:, 1].min(1)[0],
                                      masks[:, 0].max(1)[0],
                                      masks[:, 1].max(1)[0]], -1)

            mlvl_bboxes.append(bboxes)
            mlvl_scores.append(scores)
            mlvl_centerness.append(centerness)
            mlvl_masks.append(masks)

        mlvl_bboxes = torch.cat(mlvl_bboxes)
        mlvl_masks = torch.cat(mlvl_masks)
        if rescale:
            _mlvl_bboxes = mlvl_bboxes / mlvl_bboxes.new_tensor(scale_factor)
            try:
                # TODO:change cuda
                scale_factor = torch.tensor(scale_factor)[:2].cuda().unsqueeze(1).repeat(1, self.contour_points)
                _mlvl_masks = mlvl_masks / scale_factor
            except (RuntimeError, TypeError, NameError, IndexError):
                _mlvl_masks = mlvl_masks / mlvl_masks.new_tensor(scale_factor)

        mlvl_scores = torch.cat(mlvl_scores)
        padding = mlvl_scores.new_zeros(mlvl_scores.shape[0], 1)
        mlvl_scores = torch.cat([padding, mlvl_scores], dim=1)
        mlvl_centerness = torch.cat(mlvl_centerness)

        if self.mask_nms:
            '''1 mask->min_bbox->nms, performance same to origin box'''
            _mlvl_bboxes = torch.stack([_mlvl_masks[:, 0].min(1)[0],
                                        _mlvl_masks[:, 1].min(1)[0],
                                        _mlvl_masks[:, 0].max(1)[0],
                                        _mlvl_masks[:, 1].max(1)[0]], -1)
            det_bboxes, det_labels, det_masks = multiclass_nms_with_mask(
                _mlvl_bboxes,
                mlvl_scores,
                _mlvl_masks,
                cfg.score_thr,
                cfg.nms,
                cfg.max_per_img,
                score_factors=mlvl_centerness + self.centerness_factor)

        else:
            '''2 origin bbox->nms, performance same to mask->min_bbox'''
            det_bboxes, det_labels, det_masks = multiclass_nms_with_mask(
                _mlvl_bboxes,
                mlvl_scores,
                _mlvl_masks,
                cfg.score_thr,
                cfg.nms,
                cfg.max_per_img,
                score_factors=mlvl_centerness + self.centerness_factor)

        return det_bboxes, det_labels, det_masks

    # test
    def distance2mask(self, points, distances, max_shape=None, train=False, bbox=None):
        """Decode distance prediction to  mask points
        Args:
            points (Tensor): Shape (n, 2), [x, y].
            distances (Tensor): Distances from the given point to edge of contour.
            max_shape (tuple): Shape of the image.
            train (bool): set true in training mode
            bbox (bool): clamp mask predictions which are outside the predicted bbox

        Returns:
            Tensor: Decoded masks.
        """
        if self.use_fourier:
            if train:
                distances = torch.irfft(
                    torch.cat([distances, torch.zeros(distances.shape[0], self.contour_points - self.num_coe, 2,
                                                      device=points.device)], 1), 1, True, False).float().exp()

            else:
                distances = distances.reshape(-1, self.num_coe, 2)
                distances = torch.irfft(
                    torch.cat([distances[..., :self.visulize_coe, :],
                               torch.zeros(distances.shape[0], self.contour_points - self.visulize_coe, 2,
                                           device=points.device)], 1), 1, True, False).float().exp()

        angles = torch.range(0, 359, self.interval, device=points.device) / 180 * math.pi
        num_points = points.shape[0]
        points = points[:, :, None].repeat(1, 1, angles.shape[0])
        c_x, c_y = points[:, 0], points[:, 1]

        sin = torch.sin(angles)
        cos = torch.cos(angles)
        sin = sin[None, :].repeat(num_points, 1)
        cos = cos[None, :].repeat(num_points, 1)

        x = distances * sin + c_x
        y = distances * cos + c_y

        if max_shape is not None:
            x = x.clamp(min=0, max=max_shape[1] - 1)
            y = y.clamp(min=0, max=max_shape[0] - 1)
        if bbox is not None:
            x = torch.max(torch.min(x, bbox[:, 2].unsqueeze(1)), bbox[:, 0].unsqueeze(1))
            y = torch.max(torch.min(y, bbox[:, 3].unsqueeze(1)), bbox[:, 1].unsqueeze(1))

        res = torch.cat([x[:, None, :], y[:, None, :]], dim=1)
        return res, distances
