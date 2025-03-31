import numpy as np
import torch
from sam2.build_sam import build_sam2
from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator
from sam2.sam2_image_predictor import SAM2ImagePredictor
from skimage.measure import perimeter


class Segmenter:
    def __init__(self, image, checkpoint_path, model_config, device="cuda"):
        """
        Initialize the Segmenter class, generate masks with SAM for the given image.

        Args:
            image (numpy.ndarray): Image as a NumPy array in RGB format.
            checkpoint_path (str): Path to the SAM model checkpoint file.
            model_config (str): Path to the SAM model configuration file.
            device (str): Device to run the model on ("cuda" or "cpu").
        """
        # Check that the image is a valid NumPy array
        assert image is not None, "An image must be provided."
        self.image = image  # Store the image directly
        self.height = image.shape[0]
        self.width = image.shape[1]

        torch.autocast(device_type="cuda", dtype=torch.bfloat16).__enter__()

        if torch.cuda.get_device_properties(0).major >= 8:
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True

        # Device setup (use GPU if available)
        self.device = "cuda" if torch.cuda.is_available() and device == "cuda" else "cpu"

        # dinov2_vitg14 = torch.hub.load('facebookresearch/dinov2', 'dinov2_vitg14')
        # device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # dinov2_vitg14.to(device)


        # Load the SAM model
        self.sam_model = self._load_sam_model(checkpoint_path, model_config, self.device)

        # Initialize the SAM mask generator
        self.mask_generator = SAM2AutomaticMaskGenerator(model=self.sam_model,
                                                    points_per_side=64,
                                                    points_per_patch=128,
                                                    pred_iou_threshold=0.7,
                                                    stability_score_thresh=0.92,
                                                    stability_score_offset=0.7,
                                                    crop_n_layers=1,
                                                    box_nms_thresh=0.7,
                                                    )

        # Generate masks for the provided image
        self.masks = self._generate_masks()

        self.selected_masks = set()
        
        self.predictor = SAM2ImagePredictor(self.sam_model)
        self.predictor.set_image(image)

    def _load_sam_model(self, checkpoint_path, model_config, device):
        """
        Load the SAM model.

        Args:
            checkpoint_path (str): Path to the SAM checkpoint.
            model_config (str): Path to the SAM model configuration file.
            device (str): Device to load the model on ("cuda" or "cpu").

        Returns:
            torch.nn.Module: Loaded SAM model.
        """
        print("Loading SAM model...")
        model = build_sam2(
            model_config, checkpoint_path, device=device, apply_postprocessing=False
        )
        model.to(device)
        print("SAM model loaded successfully.")
        return model

    def _generate_masks(self):
        """
        Generate masks for the given image using SAM and filter out non-informative masks.

        Returns:
            list: List of generated masks for the image, excluding non-informative masks.
        """
        print("Generating masks for the provided image...")
        masks, _ = self.mask_generator.generate(self.image)

        # Image area (width x height)
        image_area = self.image.shape[0] * self.image.shape[1]

        # Threshold to determine if a mask covers "too much" of the image as a percentage
        coverage_threshold = 0.95  # Adjust based on your needs (0.95 = 95%)

        # Filter out masks that are non-informative
        filtered_masks = []
        for mask in masks:
            # Calculate mask area
            mask_area = mask['area']  # This should be available in SAM-generated masks

            # If the mask covers less than the threshold, keep it
            if (mask_area / image_area) < coverage_threshold:
                filtered_masks.append(mask)

        print(f"Detected {len(masks)} masks, kept {len(filtered_masks)} after filtering.")
        return filtered_masks

    def _compute_mask_metrics(self, mask, score):
        """
        Compute and normalize mask metrics: compactness, size penalty, and score.

        Args:
            mask (np.array): Binary mask for the segment.
            score (float): Score assigned by SAM for the mask.
            image_shape (tuple): Shape of the image as (height, width).

        Returns:
            tuple: Normalized compactness, size penalty, and score.
        """

        # Mask metrics
        mask_area = mask.sum()  # Total pixels in the mask
        mask_perimeter = perimeter(mask)  # Perimeter of the mask

        # Compactness: Avoid divide-by-zero errors
        if mask_area > 0:
            # Ideal perimeter for a circle with the same area
            ideal_perimeter = 2 * np.sqrt(np.pi * mask_area)

            # Compactness: The ratio of the perimeter to the ideal perimeter (closer to 1 is more compact)
            if mask_perimeter > 0:
                raw_compactness = ideal_perimeter / mask_perimeter  # Inverse, so lower perimeter = higher compactness
            else:
                raw_compactness = 0  # Handle the case when mask_perimeter is 0
        else:
            raw_compactness = 0

        # Normalize compactness (keeping compactness between 0 and 1)
        # Higher compactness for well-defined, continuous masks, lower for scattered/irregular ones
        compactness = min(raw_compactness, 1)  # Ensure compactness doesn't exceed 1

        # Normalized size penalty
        total_pixels = self.height * self.width

        normalized_area = mask_area / total_pixels  # Fraction of the image covered by the mask

        # Gentle penalty for very small masks (e.g., < 1% of image)
        if normalized_area < 0.001:  # Only apply penalty for masks smaller than 1% of the image
            small_mask_penalty = normalized_area ** 4  # Soft quadratic penalty
        else:
            small_mask_penalty = 0  # No penalty for larger masks

        # Large mask penalty (unchanged)
        large_mask_penalty = (normalized_area - 0.4) ** 4 if normalized_area > 0.5 else 0

        # Combine penalties gently
        size_penalty = normalized_area + small_mask_penalty + large_mask_penalty

        # Return normalized metrics
        return compactness, size_penalty, score

    def _weighted_mask_selection(self, masks, scores, weights=(1.0, 0.8, 1.4), point=None, label=None):
        best_score = -np.inf
        best_index = -1  # Initialize with an invalid index

        w_s, w_c, w_a = weights  # Weights for SAM Score, Compactness, and Size

        for i, mask in enumerate(masks):
            # Compute metrics
            compactness, size_penalty, sam_score = self._compute_mask_metrics(mask, scores[i])

            # Weighted score (nonlinear terms)
            weighted_score = (
                    w_s * sam_score +  # Higher SAM score is better
                    w_c * np.log(1 + compactness) -  # Higher compactness is better (log smoothing)
                    w_a * size_penalty  # Lower size penalty is better
            )

            # Select best mask
            if weighted_score > best_score:
                best_score = weighted_score
                best_index = i  # Store the index of the best mask

        return best_index

    def get_best_point(self):
        """
        Get the centroid of the largest unselected mask from the image.

        Returns:
            tuple: A (row, column) point representing the centroid of the largest unselected mask,
                   or None if no valid mask is found.
        """

        # Track the best candidate
        largest_mask = None
        largest_area = 0

        # Iterate over all masks to find the largest unselected mask
        for mask in self.masks:
            mask_id = id(mask)  # Use the mask's ID to uniquely identify it
            if mask_id not in self.selected_masks:
                mask_area = mask['area']  # Area of the mask
                if mask_area > largest_area:
                    largest_area = mask_area
                    largest_mask = mask

        if largest_mask is None:
            print("No unselected masks available.")
            return None  # No valid unselected masks found

        # Calculate the centroid of the largest mask
        segmentation = largest_mask['segmentation']
        indices = list(zip(*segmentation.nonzero()))  # Get row, column indices of non-zero values
        if not indices:
            print("No valid pixels in the largest mask segmentation.")
            return None  # Mask has no valid segmentation pixels

        centroid = tuple(map(int, np.mean(indices, axis=0)))  # Compute centroid as (row, column)

        # Add the largest mask to the selected set
        self.selected_masks.add(id(largest_mask))

        print(f"Selected centroid: {centroid} from the largest mask with area: {largest_area}")
        return centroid
    
    def propagate_points(self, points, labels):
        """
        Propagate points into a mask

        Args:
            points: Point prompt coordinates
            lables: Point prompt labels. 1 if positive, 0 if negative.

        Returns:
            np.array: Mask propagated from points.
        """
        
        masks, scores, logits = self.predictor.predict(
            point_coords=points,
            point_labels=labels,
            multimask_output=True,
        )

        selected_mask = self._weighted_mask_selection(masks, scores)

        mask_input = logits[selected_mask, :, :]  # Choose the model's best mask
        # mask_input = logits[np.argmax(scores), :, :]  # Choose the mask with the highest score
        masks, scores, logits = self.predictor.predict(
            point_coords=points,
            point_labels=labels,
            mask_input=mask_input[None, :, :],
            multimask_output=True,
        )

        return masks[self._weighted_mask_selection(masks, scores)]


