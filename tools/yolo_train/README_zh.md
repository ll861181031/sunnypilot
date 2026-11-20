# YOLO 训练与导出（Windows 主机）

## 安装
双击 `install_train.bat`（使用 `C:\Users\lsr\sunnypilot-yolo\venv`）。

## 训练
1. 把标注好的数据集解压到 `C:\Users\lsr\sunnypilot-yolo\dataset\batch_001`（包含 `dataset.json`、`images/`、`labels/`）。
2. 双击 `train.bat`（或 `train.bat <数据集路径>`）。
   - 默认 `yolo11n.pt`、120 epochs、640、batch 12、RTX 4070 SUPER。
   - 训练结果在 `<数据集>/runs/train/`。
   - 完成后自动导出 ONNX 并上传到 Linux `192.168.123.70:/home/lsr/yolo_models/`（paramiko，密钥同标注器）。
3. 训练前建议关闭浏览器/游戏等占显存程序；`nvidia-smi` 看剩余显存。

## 自定义
```
python train_yolo.py <dataset> --model yolo11s.pt --epochs 200 --imgsz 768 --batch 8
```
- 类别表来自 `dataset.json`，会自动生成 `data.yaml`。
- `--upload` 需要 Windows 上的 `id_ed25519_labeler` 密钥（已配好）。
- 导出的 ONNX 可回 Linux 用 `eval_video.py` 验证，或 `tools/export_ncnn.py` 转 ncnn。

## 注意
- 训练数据来自行车记录，含人脸/车牌，**不要外传**。
- 模型/权重许可：Ultralytics AGPL-3.0，与仓库 MIT 不同。

## 训练完接到 yolod（重要）

新模型是 17 类，**默认 `YOLO_CLASSES` 里的 7 个道路类不包含 e_bike/三轮类**，而且旧默认里的
`train` 在新模型里不存在。部署新模型时必须显式设置类别过滤，例如：

```bash
export YOLO_MODEL_PATH=/path/to/best.onnx
export YOLO_CLASSES=person,bicycle,e_bike,motorcycle,tricycle_cargo,tricycle_awning,tricycle_passenger,tricycle_other,car,van,truck,bus,trailer,special_vehicle,stroller,wheelchair,animal,cone,barrier,traffic_light,traffic_sign,other_obstacle
```

或 `YOLO_CLASSES=all`（会显示所有 17 类，包含噪声类）。改完重启 stack 生效。
另外 `yolod` 的 `model` 字段会显示新文件名；`classNames` 从 ONNX metadata 读取（17 类）。
