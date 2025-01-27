python run.py \
    applications.0.scheduler=fixed_pool \
    cluster=fixed-pool-test1 \
    start_state=splitwise_fixed_pool_test1 \
    start_state.split_type=heterogeneous-fixed-pool \
    performance_model=db \
    trace.filename=rr_conv_80 \
    seed=0
    #applications.0.scheduler=token_jsq \
    #trace.filename=rr_code_70 \

