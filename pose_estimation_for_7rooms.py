import os
import torch
import numpy as np
from PIL import Image
from torch.utils.data import Dataset, DataLoader
from torchvision import transforms
import torch.nn as nn
import torch.optim as optim
from torchvision.models import ResNet18_Weights, resnet18
from tqdm import tqdm
from sklearn.model_selection import KFold


class Frame:
    def __init__(self, data_type, room_label, sequence, file_name, color_image_path, pose):
        self.data_type = data_type
        self.room_label = room_label
        self.sequence = sequence
        self.file_name = file_name
        self.color_image_path = color_image_path
        self.pose = pose


def parse_pose_file(pose_file_path):
    with open(pose_file_path, 'r') as file:
        pose = np.array([list(map(float, line.strip().split())) for line in file]).flatten()
    return pose


def create_frame_objects(data_path, room_name_create_frame, data_type):
    frames = []
    for seq_folder in os.listdir(data_path):
        seq_path = os.path.join(data_path, seq_folder)
        if os.path.isdir(seq_path):
            print(f"Processing sequence: {seq_folder} in {room_name_create_frame} ({data_type})")
            for frame_file in os.listdir(seq_path):
                if frame_file.endswith('.color.png'):
                    frame_name = frame_file.split('.')[0]
                    color_image_path = os.path.join(seq_path, f"{frame_name}.color.png")
                    pose_file_path = os.path.join(seq_path, f"{frame_name}.pose.txt")
                    if os.path.exists(color_image_path) and os.path.exists(pose_file_path):
                        pose = parse_pose_file(pose_file_path)
                        frame = Frame(data_type, room_name_create_frame, seq_folder, frame_name, color_image_path, pose)
                        frames.append(frame)
    return frames


def create_data_structure(data_folder):
    local_train_data = []
    local_test_data = []
    room_names_list = ['chess', 'fire', 'heads', 'office', 'pumpkin', 'redkitchen', 'stairs']
    for room in room_names_list:
        train_path = os.path.join(data_folder, room, 'train')
        test_path = os.path.join(data_folder, room, 'test')
        local_train_data.extend(create_frame_objects(train_path, room, 'train'))
        local_test_data.extend(create_frame_objects(test_path, room, 'test'))
    return local_train_data, local_test_data


your_path_to_data_folder = '/Volumes/MERT SSD/data'
# train_data, test_data = create_data_structure(your_path_to_data_folder)


class CustomDataset(Dataset):
    def __init__(self, frames, transform=None):
        self.frames = frames
        self.transform = transform

    def __len__(self):
        return len(self.frames)

    def __getitem__(self, idx):
        frame = self.frames[idx]
        image = Image.open(frame.color_image_path).convert('RGB')
        pose_matrix = np.array(frame.pose, dtype=np.float32).reshape(4, 4)
        translation = pose_matrix[:3, 3]
        rotation = pose_matrix[:3, :3]
        if self.transform:
            image = self.transform(image)
        return image, torch.from_numpy(translation), torch.from_numpy(rotation.flatten())


transformations = transforms.Compose([
    transforms.Resize((224, 224)),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class PoseModel(nn.Module):
    def __init__(self):
        super(PoseModel, self).__init__()
        weights = ResNet18_Weights.DEFAULT
        self.backbone = resnet18(weights=weights)
        self.fc_translation = nn.Linear(self.backbone.fc.in_features, 3)
        self.fc_rotation = nn.Linear(self.backbone.fc.in_features, 9)
        self.backbone.fc = nn.Identity()

    def forward(self, x):
        features = self.backbone(x)
        translation = self.fc_translation(features)
        rotation = self.fc_rotation(features)
        return translation, rotation


def rotation_matrix_to_angle_axis(rotation_matrices):
    """Convert a batch of rotation matrices to angle-axis vectors."""
    # Calculate the trace of each 3x3 rotation matrix in the batch
    traces = torch.einsum('bii->b', rotation_matrices)  # Sum over the diagonal elements in each matrix in the batch
    cos_thetas = (traces - 1) / 2.0
    cos_thetas = torch.clamp(cos_thetas, -1, 1)  # Numerical errors might make cos(theta) slightly out of its range
    thetas = torch.acos(cos_thetas)  # Angles

    # Initialize angle-axis vectors
    angle_axes = torch.zeros_like(rotation_matrices[:, :, 0])

    # Compute sin(theta) for normalization
    sin_thetas = torch.sin(thetas)

    # Find indices where theta is not too small (to avoid division by zero)
    valid = sin_thetas > 1e-5

    # For valid indices where theta is not too small, calculate angle-axis vectors
    angle_axes[valid] = torch.stack([
        rotation_matrices[valid, 2, 1] - rotation_matrices[valid, 1, 2],
        rotation_matrices[valid, 0, 2] - rotation_matrices[valid, 2, 0],
        rotation_matrices[valid, 1, 0] - rotation_matrices[valid, 0, 1]
    ], dim=1) / (2 * sin_thetas[valid].unsqueeze(1)) * thetas[valid].unsqueeze(1)

    return angle_axes


def rotation_error(pred_rot, gt_rot):
    """Calculate the angular distance between two rotation matrices."""
    pred_rot_matrix = pred_rot.view(-1, 3, 3)
    gt_rot_matrix = gt_rot.view(-1, 3, 3)
    r_diff = torch.matmul(pred_rot_matrix, gt_rot_matrix.transpose(1, 2))  # Relative rotation
    angle_axis = rotation_matrix_to_angle_axis(r_diff)
    return torch.norm(angle_axis, dim=1)  # Returns the magnitude of the angle-axis vector


def calculate_translation_error(pred, target):
    return torch.norm(pred - target, dim=1).mean()


pose_model = PoseModel().to(device)
optimizer = optim.SGD(pose_model.parameters(), lr=0.001, momentum=0.9)
criterion = nn.MSELoss()


def train_and_evaluate(room_name, train_data, test_data):
    train_dataset = CustomDataset(train_data, transform=transformations)
    test_dataset = CustomDataset(test_data, transform=transformations)
    train_loader = DataLoader(train_dataset, batch_size=32, shuffle=True, num_workers=4)
    test_loader = DataLoader(test_dataset, batch_size=32, shuffle=False, num_workers=4)

    n_splits = 5
    num_epochs = 12
    best_loss = float('inf')
    best_model_state = None
    dataset_size = len(train_loader.dataset)  # warning is disregarded, since the code works correct
    indices = range(dataset_size)

    kfold = KFold(n_splits=n_splits, shuffle=True, random_state=42)

    for fold, (train_idx, val_idx) in enumerate(kfold.split(indices)):
        print(f"Processing fold {fold} for room {room_name}")
        train_subsampler = torch.utils.data.SubsetRandomSampler(train_idx)
        val_subsampler = torch.utils.data.SubsetRandomSampler(val_idx)

        current_train_loader = DataLoader(train_dataset, batch_size=32, sampler=train_subsampler, num_workers=4)
        val_loader = DataLoader(train_dataset, batch_size=32, sampler=val_subsampler, num_workers=4)

        for epoch in tqdm(range(num_epochs), desc="Epochs Progress"):
            pose_model.train()
            total_loss = 0.0
            total_translation_error = 0.0
            total_rotation_error = 0.0

            # Training loop
            for images, translations, rotations in tqdm(current_train_loader,
                                                        desc=f"Training Epoch {epoch + 1}", leave=False):
                images = images.to(device)
                translations = translations.to(device)
                rotations = rotations.view(-1, 3, 3).to(device)

                optimizer.zero_grad()
                trans_pred, rot_pred = pose_model(images)
                rot_pred = rot_pred.view(-1, 3, 3)

                loss_translation = criterion(trans_pred, translations)
                loss_rotation = criterion(rot_pred.view(-1, 9), rotations.view(-1, 9))
                loss = loss_translation + loss_rotation
                loss.backward()
                optimizer.step()

                total_loss += loss.item()
                translation_error = calculate_translation_error(trans_pred, translations)
                total_translation_error += translation_error.item()
                rotation_error_batch = rotation_error(rot_pred, rotations).mean().item()
                total_rotation_error += rotation_error_batch

            # Validation loop
            pose_model.eval()
            validation_loss = 0.0
            with torch.no_grad():
                for images, translations, rotations in tqdm(val_loader,
                                                            desc=f"Validating Epoch {epoch + 1}",
                                                            leave=False):
                    images = images.to(device)
                    translations = translations.to(device)
                    rotations = rotations.view(-1, 3, 3).to(device)

                    trans_pred, rot_pred = pose_model(images)
                    rot_pred = rot_pred.view(-1, 3, 3)

                    loss_translation = criterion(trans_pred, translations)
                    loss_rotation = criterion(rot_pred.view(-1, 9), rotations.view(-1, 9))
                    loss = loss_translation + loss_rotation
                    validation_loss += loss.item()

            validation_loss /= len(val_loader)

            if validation_loss < best_loss:
                best_loss = validation_loss
                best_model_state = pose_model.state_dict()  # Update best model state
                print(f"New best model for {room_name} in fold {fold} with loss {best_loss:.4f}")

    # Save the best model for the room
    if best_model_state:
        torch.save(best_model_state, f'best_pose_model_{room_name}.pth')
        print(f"Best model saved for {room_name} with loss {best_loss:.4f}")

    # To use the best model
    pose_model.load_state_dict(torch.load(f'best_pose_model_{room_name}.pth'))
    pose_model.eval()

    # Initialize metrics
    total_translation_error = 0.0
    total_rotation_error = 0.0
    count = 0

    # No gradient needed for evaluation
    with torch.no_grad():
        for images, translations, rotations in test_loader:
            images = images.to(device)
            translations = translations.to(device)
            rotations = rotations.view(-1, 3, 3).to(device)  # 3x3 rotation matrices

            # Predict
            trans_pred, rot_pred = pose_model(images)
            rot_pred = rot_pred.view(-1, 3, 3)

            # Calculate errors
            translation_error = calculate_translation_error(trans_pred, translations)
            rotation_error_batch = rotation_error(rot_pred, rotations).mean().item()

            # Aggregate errors
            total_translation_error += translation_error.item()
            total_rotation_error += rotation_error_batch
            count += 1

        # Calculate average errors
        average_translation_error = total_translation_error / count
        average_rotation_error = total_rotation_error / count

        print(f"Performance of the best model for {room_name} on the test data:")
        print(f"Average Translation Error: {average_translation_error:.4f} meters")
        print(f"Average Rotation Error (radians): {average_rotation_error:.4f}")
        print("-" * 50)


scene_names = ['chess', 'fire', 'heads', 'office', 'pumpkin', 'redkitchen', 'stairs']
for scene in scene_names:
    train_data_main = create_frame_objects(your_path_to_data_folder, scene, 'train')
    print(train_data_main)
    test_data_main = create_frame_objects(your_path_to_data_folder, scene, 'test')
    print(test_data_main)
    train_and_evaluate(scene, train_data_main, test_data_main)