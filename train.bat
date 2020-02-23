set CUDA_VISIBLE_DEVICES=0
python run_summarization.py ^
    --mode=train ^
    --data_path=data/train.txt ^
    --vocab_path=data/vocab.txt ^
    --log_root=log ^
    --exp_name=extractive ^
    --vocab_size=4000 ^
    --coverage=0 ^
    --batch_size=128 ^
    --convert_to_coverage_model=0 ^
    --restore_best_model=0
