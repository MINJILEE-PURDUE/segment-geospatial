import os
import warnings
import argparse
import numpy as np
import torch
from PIL import Image
from segment_anything import sam_model_registry
from segment_anything import SamPredictor
from .common import *

try:
    import rasterio
except ImportError:
    print("Installing rasterio...")
    install_package("rasterio")

warnings.filterwarnings("ignore")


try:
    import groundingdino.datasets.transforms as T
    from groundingdino.models import build_model
    from groundingdino.util import box_ops
    from groundingdino.util.inference import predict
    from groundingdino.util.slconfig import SLConfig
    from groundingdino.util.utils import clean_state_dict
    from huggingface_hub import hf_hub_download
except ImportError:
    print("Installing GroundingDINO...")
    install_package("https://github.com/IDEA-Research/GroundingDINO")
    print("Please restart the kernel and run the notebook again.")


SAM_MODELS = {
    "vit_h": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_h_4b8939.pth",
    "vit_l": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_l_0b3195.pth",
    "vit_b": "https://dl.fbaipublicfiles.com/segment_anything/sam_vit_b_01ec64.pth",
}

CACHE_PATH = os.environ.get(
    "TORCH_HOME", os.path.expanduser("~/.cache/torch/hub/checkpoints")
)


def load_model_hf(repo_id, filename, ckpt_config_filename, device="cpu"):
    cache_config_file = hf_hub_download(repo_id=repo_id, filename=ckpt_config_filename)
    args = SLConfig.fromfile(cache_config_file)
    model = build_model(args)
    model.to(device)
    cache_file = hf_hub_download(repo_id=repo_id, filename=filename)
    checkpoint = torch.load(cache_file, map_location="cpu")
    model.load_state_dict(clean_state_dict(checkpoint["model"]), strict=False)
    model.eval()
    return model


def transform_image(image) -> torch.Tensor:
    transform = T.Compose(
        [
            T.RandomResize([800], max_size=1333),
            T.ToTensor(),
            T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    image_transformed, _ = transform(image, None)
    return image_transformed


# Class definition for LangSAM
class LangSAM:
    def __init__(self, model_type="vit_h"):


        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.build_groundingdino()
        self.build_sam(model_type)

        self.source = None
        self.image = None
        self.masks = None
        self.boxes = None
        self.phrases = None
        self.logits = None
        self.prediction = None

    def build_sam(self, model_type):
        checkpoint_url = SAM_MODELS[model_type]
        sam = sam_model_registry[model_type]()
        state_dict = torch.hub.load_state_dict_from_url(checkpoint_url)
        sam.load_state_dict(state_dict, strict=True)
        sam.to(device=self.device)
        self.sam = SamPredictor(sam)

    def build_groundingdino(self):
        ckpt_repo_id = "ShilongLiu/GroundingDINO"
        ckpt_filename = "groundingdino_swinb_cogcoor.pth"
        ckpt_config_filename = "GroundingDINO_SwinB.cfg.py"
        self.groundingdino = load_model_hf(
            ckpt_repo_id, ckpt_filename, ckpt_config_filename, self.device
        )

    def predict_dino(self, image, text_prompt, box_threshold, text_threshold):
        image_trans = transform_image(image)
        boxes, logits, phrases = predict(
            model=self.groundingdino,
            image=image_trans,
            caption=text_prompt,
            box_threshold=box_threshold,
            text_threshold=text_threshold,
            device=self.device,
        )
        W, H = image.size
        boxes = box_ops.box_cxcywh_to_xyxy(boxes) * torch.Tensor([W, H, W, H])

        return boxes, logits, phrases

    def predict_sam(self, image, boxes):
        image_array = np.asarray(image)
        self.sam.set_image(image_array)
        transformed_boxes = self.sam.transform.apply_boxes_torch(
            boxes, image_array.shape[:2]
        )
        masks, _, _ = self.sam.predict_torch(
            point_coords=None,
            point_labels=None,
            boxes=transformed_boxes.to(self.sam.device),
            multimask_output=False,
        )
        return masks.cpu()

    def predict(
        self,
        image,
        text_prompt,
        box_threshold,
        text_threshold,
        output=None,
        mask_multiplier=255,
        dtype=np.uint8,
        save_args={},
        return_results=False,
        **kwargs,
    ):

        if isinstance(image, str):
            if image.startswith("http"):
                image = download_file(image)

            if not os.path.exists(image):
                raise ValueError(f"Input path {image} does not exist.")

            self.source = image

            # Load the georeferenced image
            with rasterio.open(image) as src:
                image_np = src.read().transpose((1, 2, 0))  # Convert rasterio image to numpy array
                transform = src.transform  # Save georeferencing information
                crs = src.crs  # Save the Coordinate Reference System
                image_pil = Image.fromarray(image_np[:, :, :3])  # Convert numpy array to PIL image, excluding the alpha channel
        else:
            image_pil = image

        self.image = image_pil

        boxes, logits, phrases = self.predict_dino(
            image_pil, text_prompt, box_threshold, text_threshold
        )
        masks = torch.tensor([])
        if len(boxes) > 0:
            masks = self.predict_sam(image_pil, boxes)
            masks = masks.squeeze(1)

        if boxes.nelement() == 0:  # No "object" instances found
            print('No objects found in the image.')
            return
        else:
            # Create an empty image to store the mask overlays
            mask_overlay = np.zeros_like(image_np[..., 0], dtype=dtype)  # Adjusted for single channel

            for i, (box, mask) in enumerate(zip(boxes, masks)):
                # Convert tensor to numpy array if necessary and ensure it contains integers
                if isinstance(mask, torch.Tensor):
                    mask = mask.cpu().numpy().astype(dtype)  # If mask is on GPU, use .cpu() before .numpy()
                mask_overlay += ((mask > 0) * (i + 1)).astype(dtype)  # Assign a unique value for each mask

            # Normalize mask_overlay to be in [0, 255]
            mask_overlay = (mask_overlay > 0) * mask_multiplier  # Binary mask in [0, 255]


        if output is not None:
            array_to_image(mask_overlay, output, self.source, dtype=dtype, **save_args)

        self.masks = masks
        self.boxes = boxes
        self.phrases = phrases
        self.logits = logits
        self.prediction = mask_overlay

        if return_results:
            return masks, boxes, phrases, logits

    def show_anns(
        self,
        figsize=(12, 10),
        axis="off",
        cmap='viridis', 
        alpha=0.4,
        add_boxes=True,
        box_color='r',
        box_linewidth=1,
        title=None,
        output=None,
        blend=True,
        **kwargs,
    ):
        """Show the annotations (objects with random color) on the input image.

        Args:
            figsize (tuple, optional): The figure size. Defaults to (12, 10).
            axis (str, optional): Whether to show the axis. Defaults to "off".
            alpha (float, optional): The alpha value for the annotations. Defaults to 0.35.
            output (str, optional): The path to the output image. Defaults to None.
            blend (bool, optional): Whether to show the input image. Defaults to True.
        """

        import warnings
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches

        warnings.filterwarnings("ignore")

        anns = self.prediction

        if anns is None:
            print("Please run predict() first.")
            return
        elif len(anns) == 0:
            print('No objects found in the image.')
            return

        plt.figure(figsize=figsize)
        plt.imshow(self.image)

        if add_boxes:

            for box in self.boxes:
                # Draw bounding box
                box = box.cpu().numpy()  # Convert the tensor to a numpy array
                rect = patches.Rectangle((box[0], box[1]), box[2] - box[0], box[3] - box[1], linewidth=box_linewidth, edgecolor=box_color, facecolor='none')
                plt.gca().add_patch(rect)

        if "dpi" not in kwargs:
            kwargs["dpi"] = 100

        if "bbox_inches" not in kwargs:
            kwargs["bbox_inches"] = "tight"

        plt.imshow(anns, cmap=cmap, alpha=alpha)

        if title is not None:
            plt.title(title)
        plt.axis(axis)

        if output is not None:
            if blend:
                array = blend_images(
                    self.prediction, self.image, alpha=alpha, show=False, **kwargs
                )
            else:
                array = self.prediction
            array_to_image(array, output, self.source)


def main():
    parser = argparse.ArgumentParser(description="LangSAM")
    parser.add_argument("--image", required=True, help="path to the image")
    parser.add_argument("--prompt", required=True, help="text prompt")
    parser.add_argument(
        "--box_threshold", default=0.5, type=float, help="box threshold"
    )
    parser.add_argument(
        "--text_threshold", default=0.5, type=float, help="text threshold"
    )
    args = parser.parse_args()

    with rasterio.open(args.image) as src:
        image_np = src.read().transpose(
            (1, 2, 0)
        )  # Convert rasterio image to numpy array
        transform = src.transform  # Save georeferencing information
        crs = src.crs  # Save the Coordinate Reference System

    model = LangSAM()

    image_pil = Image.fromarray(
        image_np[:, :, :3]
    )  # Convert numpy array to PIL image, excluding the alpha channel
    image_np_copy = image_np.copy()  # Create a copy for modifications

    masks, boxes, phrases, logits = model.predict(
        image_pil, args.prompt, args.box_threshold, args.text_threshold
    )

    if boxes.nelement() == 0:  # No "object" instances found
        print("No objects found in the image.")
    else:
        # Create an empty image to store the mask overlays
        mask_overlay = np.zeros_like(
            image_np[..., 0], dtype=np.int64
        )  # Adjusted for single channel

        for i in range(len(boxes)):
            box = boxes[i].cpu().numpy()  # Convert the tensor to a numpy array
            mask = masks[i].cpu().numpy()  # Convert the tensor to a numpy array

            # Add the mask to the mask_overlay image
            mask_overlay += (mask > 0) * (i + 1)  # Assign a unique value for each mask

    # Normalize mask_overlay to be in [0, 255]
    mask_overlay = ((mask_overlay > 0) * 255).astype(
        rasterio.uint8
    )  # Binary mask in [0, 255]

    with rasterio.open(
        "mask.tif",
        "w",
        driver="GTiff",
        height=mask_overlay.shape[0],
        width=mask_overlay.shape[1],
        count=1,
        dtype=mask_overlay.dtype,
        crs=crs,
        transform=transform,
    ) as dst:
        dst.write(mask_overlay, 1)


if __name__ == "__main__":
    main()
