import torch
import torch.nn as nn

class CSPBlock(nn.Module):
    def __init__(self, in_channels, out_channels, num_blocks):
        super(CSPBlock, self).__init__()
        self.main_branch = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 1, 1, 0, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.LeakyReLU(0.1)
        )
        self.shortcut_branch = nn.Conv2d(in_channels, out_channels, 1, 1, 0, bias=False)
        
        self.blocks = nn.Sequential(
            *[nn.Sequential(
                nn.Conv2d(out_channels, out_channels, 3, 1, 1, bias=False),
                nn.BatchNorm2d(out_channels),
                nn.LeakyReLU(0.1)
            ) for _ in range(num_blocks)]
        )
        
        self.concat_conv = nn.Conv2d(out_channels * 2, out_channels, 1, 1, 0, bias=False)

    def forward(self, x):
        main = self.main_branch(x)
        shortcut = self.shortcut_branch(x)
        main = self.blocks(main)
        x = torch.cat([main, shortcut], dim=1)
        return self.concat_conv(x)
    
import torch
import torch.nn as nn
import torch.nn.functional as F

class FPN(nn.Module):
    def __init__(self, in_channels_list, out_channels):
        super(FPN, self).__init__()
        # Lateral convolutions for each level
        self.lateral_convs = nn.ModuleList([
            nn.Conv2d(in_channels, out_channels, kernel_size=1) for in_channels in in_channels_list
        ])
        # FPN convolutions for each level
        self.fpn_convs = nn.ModuleList([
            nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1) for _ in in_channels_list
        ])
        # Channel alignment for top-down pathway
        self.channel_align = nn.ModuleList([
            nn.Conv2d(out_channels, out_channels, kernel_size=1) for _ in in_channels_list
        ])

    def forward(self, features):
        print(f"FPN input features: {[f.shape for f in features]}")

        # Process the last feature map (smallest resolution)
        last_inner = self.lateral_convs[-1](features[-1])
        print(f"Last inner shape (initial): {last_inner.shape}")

        results = [self.fpn_convs[-1](last_inner)]

        for i in range(len(features) - 2, -1, -1):
            # Upsample and align the top-down feature map
            inner_top_down = F.interpolate(last_inner, size=features[i].shape[-2:], mode="nearest")
            inner_top_down = self.channel_align[i](inner_top_down)  # Align channels

            # Process the lateral feature map
            inner_lateral = self.lateral_convs[i](features[i])
            print(f"Inner Top-Down shape: {inner_top_down.shape}, Inner Lateral shape: {inner_lateral.shape}")

            # Combine lateral and top-down features
            last_inner = inner_lateral + inner_top_down
            results.insert(0, self.fpn_convs[i](last_inner))

        print(f"FPN output features: {[r.shape for r in results]}")
        return results


import torch
import torch.nn as nn
from models.CSPnet import CSPBlock
from models.PAnet import PANet
from models.FPN import FPN
from models.PostProcessor import YOLOPostProcessor

class YOLOv4Backbone(nn.Module):
    def __init__(self):
        super(YOLOv4Backbone, self).__init__()
        self.csp1 = CSPBlock(3, 32, 1)       # 채널 크기 감소
        self.csp2 = CSPBlock(32, 64, 1)      # 채널 크기 감소, 블록 수 감소
        self.csp3 = CSPBlock(64, 128, 2)     # 채널 크기 감소, 블록 수 감소
        self.csp4 = CSPBlock(128, 256, 2)    # 채널 크기 감소, 블록 수 감소
        self.csp5 = CSPBlock(256, 512, 1)    # 채널 크기 감소, 블록 수 감소

    
    def forward(self, x):
        out1 = self.csp1(x)
        out2 = self.csp2(out1)
        out3 = self.csp3(out2)  # 작은 해상도
        out4 = self.csp4(out3)  # 중간 해상도
        out5 = self.csp5(out4)  # 가장 작은 해상도
        print(f"out3: {out3.shape}, out4: {out4.shape}, out5: {out5.shape}")
        return out3, out4, out5  # FPN에 전달

class YOLOv4Head(nn.Module):
    def __init__(self, num_classes):
        super(YOLOv4Head, self).__init__()
        self.fpn = FPN([128, 256, 512], 128)  # FPN 경량화
        self.pan = PANet([128, 128, 128], 128)  # PANet 경량화
        self.num_classes = num_classes
        self.yolo_layers = nn.ModuleList([
            nn.Conv2d(128, 3 * (num_classes + 5), 1) for _ in range(3)  # 3개의 스케일별 YOLO 레이어
        ])

    def forward(self, features):
        # FPN
        fpn_features = self.fpn(features)
        print(f"FPN outputs: {[f.shape for f in fpn_features]}")

        # PANet
        pan_features = self.pan(fpn_features)
        outputs = []

        for layer, p in zip(self.yolo_layers, pan_features):
            out = layer(p)  # [batch, 3 * (num_classes + 5), H, W]
            batch_size, _, h, w = out.shape
            # YOLO 형식으로 reshape
            out = out.view(batch_size, 3, self.num_classes + 5, h, w).permute(0, 1, 3, 4, 2)
            # 시그모이드 활성화
            out[..., 4] = torch.sigmoid(out[..., 4])  # Confidence score
            out[..., 5:] = torch.sigmoid(out[..., 5:])  # Class probabilities
            outputs.append(out)

        return outputs  # [scale1, scale2, scale3]


class YOLOv4(nn.Module):
    def __init__(self, num_classes, input_dim=416, conf_threshold=0.5, iou_threshold=0.4):
        super(YOLOv4, self).__init__()
        self.backbone = YOLOv4Backbone()
        self.head = YOLOv4Head(num_classes)
        self.post_processor = YOLOPostProcessor(conf_threshold=conf_threshold, iou_threshold=iou_threshold, input_dim=input_dim)

    def forward(self, x, original_img_shape):
        print("Starting Backbone...")
        features = self.backbone(x)
        print(f"Backbone outputs: {[f.shape for f in features]}")

        print("Starting Head...")
        outputs = self.head(features)
        print(f"Head outputs: {[o.shape for o in outputs]}")

        print("Concatenating outputs...")
        raw_predictions = torch.cat(outputs, dim=1)
        print(f"Raw predictions shape: {raw_predictions.shape}")

        print("Post-processing...")
        final_boxes = self.post_processor.process_predictions(raw_predictions, original_img_shape)
        print("Forward pass completed.")
        return final_boxes


import torch
from torchvision.ops import nms

class YOLOPostProcessor:
    def __init__(self, conf_threshold=0.3, iou_threshold=0.4, input_dim=256):
        """
        YOLO 모델의 추론 결과를 처리하는 PostProcessor.
        
        Args:
            conf_threshold (float): 신뢰도 점수 임계값 (default=0.3)
            iou_threshold (float): NMS에 사용할 IoU 임계값 (default=0.4)
            input_dim (int): 모델 입력 크기 (default=256)
        """
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.input_dim = input_dim

    def process_predictions(self, predictions, original_img_shape):
        """
        YOLO 모델의 원본 예측 결과를 후처리하여 최종 바운딩 박스 생성.

        Args:
            predictions (torch.Tensor): YOLO 모델의 출력. Shape: [batch_size, num_predictions, num_classes + 5]
            original_img_shape (tuple): 원본 이미지 크기 (height, width)

        Returns:
            list: 최종 바운딩 박스 리스트. 각 박스는 [x, y, w, h, confidence, class_id, class_score].
        """
        # 디버깅: original_img_shape 출력
        #print(f"Processing predictions with original image shape: {original_img_shape}")

        batch_boxes = []
        for batch_idx, pred in enumerate(predictions):
            # print(f"Processing batch {batch_idx}, raw predictions shape: {pred.shape}")

            # Filter boxes
            filtered_boxes = self.filter_boxes(pred)
            # print(f"Batch {batch_idx} - Filtered boxes count: {len(filtered_boxes)}")

            if len(filtered_boxes) == 0:
                # print(f"No boxes left after filtering for batch {batch_idx}. Skipping...")
                batch_boxes.append([])
                continue

            # Rescale boxes
            rescaled_boxes = self.rescale_boxes(filtered_boxes, original_img_shape)
            # print(f"Batch {batch_idx} - Rescaled boxes count: {len(rescaled_boxes)}")

            if len(rescaled_boxes) == 0:
                # print(f"No boxes left after rescaling for batch {batch_idx}. Skipping...")
                batch_boxes.append([])
                continue

            # Non-Max Suppression
            nms_boxes = self.non_max_suppression(rescaled_boxes)
            # print(f"Batch {batch_idx} - NMS boxes count: {len(nms_boxes)}")

            batch_boxes.append(nms_boxes)
        return batch_boxes

    def filter_boxes(self, predictions, max_boxes=3000):
        """
        신뢰도 점수가 일정 기준 이상인 바운딩 박스를 필터링.
        
        Args:
            predictions (torch.Tensor): 예측 결과.
            max_boxes (int): 유지할 최대 박스 수.
        
        Returns:
            list: 필터링된 바운딩 박스.
        """
        mask = predictions[..., 4] > self.conf_threshold
        # print(f"Confidence scores: min={predictions[..., 4].min()}, max={predictions[..., 4].max()}, mean={predictions[..., 4].mean()}")
        # print(f"Mask shape: {mask.shape}, Passed boxes: {mask.sum().item()}")

        filtered_preds = predictions[mask]

        # 상위 max_boxes 제한
        if len(filtered_preds) > max_boxes:
            _, indices = filtered_preds[..., 4].topk(max_boxes)
            filtered_preds = filtered_preds[indices]

        boxes = []
        for pred in filtered_preds:
            x, y, w, h = pred[:4]
            conf = pred[4]
            class_scores = pred[5:]
            class_id = class_scores.argmax().item()
            class_score = class_scores[class_id].item()
            boxes.append([x, y, w, h, conf, class_id, class_score])
        return boxes

    def rescale_boxes(self, boxes, original_img_shape):
        """
        모델 입력 크기에서 원본 이미지 크기로 바운딩 박스 크기를 조정.
        
        Args:
            boxes (list): 바운딩 박스 리스트.
            original_img_shape (tuple): 원본 이미지 크기 (height, width).
        
        Returns:
            list: 원본 이미지 크기에 맞게 조정된 바운딩 박스 리스트.
        """
        orig_h, orig_w = original_img_shape
        scale_factor = min(self.input_dim / orig_w, self.input_dim / orig_h)

        scaled_boxes = []
        for box in boxes:
            x = (box[0] - (self.input_dim - scale_factor * orig_w) / 2) / scale_factor
            y = (box[1] - (self.input_dim - scale_factor * orig_h) / 2) / scale_factor
            w = box[2] / scale_factor
            h = box[3] / scale_factor

            # 좌표 범위 제한 (0 이상)
            x = max(0, x)
            y = max(0, y)
            scaled_boxes.append([x, y, w, h, box[4], box[5], box[6]])
        return scaled_boxes

    def non_max_suppression(self, boxes):
        """
        Non-Maximum Suppression (NMS) 알고리즘을 통해 중복 바운딩 박스 제거.
        
        Args:
            boxes (list): 바운딩 박스 리스트.
        
        Returns:
            list: NMS를 통과한 바운딩 박스 리스트.
        """
        if len(boxes) == 0:
            return []

        boxes_tensor = torch.tensor(boxes)
        xywh_boxes = boxes_tensor[:, :4]
        scores = boxes_tensor[:, 4]
        indices = nms(xywh_boxes, scores, self.iou_threshold)
        return boxes_tensor[indices].tolist()

    def calculate_iou(self, box1, box2):
        """
        두 바운딩 박스 간 IoU(Intersection over Union) 계산.
        
        Args:
            box1 (list): 첫 번째 박스 [x, y, w, h, ...].
            box2 (list): 두 번째 박스 [x, y, w, h, ...].
        
        Returns:
            float: IoU 값.
        """
        x1 = max(box1[0], box2[0])
        y1 = max(box1[1], box2[1])
        x2 = min(box1[0] + box1[2], box2[0] + box2[2])
        y2 = min(box1[1] + box1[3], box2[1] + box2[3])

        inter_area = max(0, x2 - x1) * max(0, y2 - y1)
        box1_area = box1[2] * box1[3]
        box2_area = box2[2] * box2[3]

        return inter_area / (box1_area + box2_area - inter_area + 1e-6)

