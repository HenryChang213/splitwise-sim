python run.py \
    applications.0.scheduler=fixed_pool \
    cluster=fixed-pool-test \
    start_state=splitwise_fixed_pool_test \
    start_state.split_type=heterogeneous-fixed-pool \
    performance_model=db \
    trace.filename=rr_code_100_2min \
    seed=0


