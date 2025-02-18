import os
import subprocess

from garaga.modulo_circuit_structs import G1PointCircuit
from garaga.starknet.cli.utils import create_directory
from garaga.starknet.groth16_contract_generator.generator import (
    ECIP_OPS_CLASS_HASH,
    get_scarb_toml_file,
    precompute_lines_from_vk,
)
from garaga.starknet.groth16_contract_generator.parsing_utils import (
    RISC0_BN254_CONTROL_ID,
    RISC0_CONTROL_ROOT,
    Groth16VerifyingKey,
    split_digest,
)


def gen_risc0_groth16_verifier(
    vk_path: str,
    output_folder_path: str,
    output_folder_name: str,
    ecip_class_hash: int = ECIP_OPS_CLASS_HASH,
    control_root: int = RISC0_CONTROL_ROOT,
    control_id: int = RISC0_BN254_CONTROL_ID,
    cli_mode: bool = False,
) -> str:
    vk = Groth16VerifyingKey.from_json(vk_path)
    curve_id = vk.curve_id
    output_folder_name = output_folder_name + f"_{curve_id.name.lower()}"
    output_folder_path = os.path.join(output_folder_path, output_folder_name)

    precomputed_lines = precompute_lines_from_vk(vk)
    CONTROL_ROOT_0, CONTROL_ROOT_1 = split_digest(control_root)

    T = G1PointCircuit.from_G1Point(
        name="T",
        point=(vk.ic[1].scalar_mul(CONTROL_ROOT_0))
        .add(vk.ic[2].scalar_mul(CONTROL_ROOT_1))
        .add(vk.ic[5].scalar_mul(control_id).add(vk.ic[0])),
    )

    constants_code = f"""
    use garaga::definitions::{{G1Point, G2Point, E12D, G2Line, u384}};
    use garaga::groth16::Groth16VerifyingKey;

    pub const N_FREE_PUBLIC_INPUTS:usize = 2;
    // CONTROL ROOT USED : {hex(control_root)}
    // \t CONTROL_ROOT_0 : {hex(CONTROL_ROOT_0)}
    // \t CONTROL_ROOT_1 : {hex(CONTROL_ROOT_1)}
    // BN254 CONTROL ID USED : {hex(control_id)}
    pub const T: G1Point = {T.serialize(raw=True)}; // IC[0] + IC[1] * CONTROL_ROOT_0 + IC[2] * CONTROL_ROOT_1 + IC[5] * BN254_CONTROL_ID
    {vk.serialize_to_cairo()}
    pub const precomputed_lines: [G2Line; {len(precomputed_lines)//4}] = {precomputed_lines.serialize(raw=True, const=True)};
    """

    contract_code = f"""
use garaga::definitions::E12DMulQuotient;
use garaga::groth16::{{Groth16ProofRaw, MPCheckHint{curve_id.name}}};
use super::groth16_verifier_constants::{{N_FREE_PUBLIC_INPUTS, vk, ic, precomputed_lines, T}};

#[starknet::interface]
trait IRisc0Groth16Verifier{curve_id.name}<TContractState> {{
    fn verify_groth16_proof_{curve_id.name.lower()}(
        ref self: TContractState,
        groth16_proof: Groth16ProofRaw,
        image_id: Span<u32>,
        journal_digest: Span<u32>,
        mpcheck_hint: MPCheckHint{curve_id.name},
        small_Q: E12DMulQuotient,
        msm_hint: Array<felt252>,
    ) -> bool;
}}

#[starknet::contract]
mod Risc0Groth16Verifier{curve_id.name} {{
    use starknet::SyscallResultTrait;
    use garaga::definitions::{{G1Point, G1G2Pair, E12DMulQuotient}};
    use garaga::groth16::{{multi_pairing_check_{curve_id.name.lower()}_3P_2F_with_extra_miller_loop_result, Groth16ProofRaw, MPCheckHint{curve_id.name}}};
    use garaga::ec_ops::{{G1PointTrait, G2PointTrait, ec_safe_add}};
    use garaga::risc0_utils::compute_receipt_claim;
    use super::{{N_FREE_PUBLIC_INPUTS, vk, ic, precomputed_lines, T}};

    const ECIP_OPS_CLASS_HASH: felt252 = {hex(ecip_class_hash)};
    use starknet::ContractAddress;

    #[storage]
    struct Storage {{}}

    #[abi(embed_v0)]
    impl IRisc0Groth16Verifier{curve_id.name} of super::IRisc0Groth16Verifier{curve_id.name}<ContractState> {{
        fn verify_groth16_proof_{curve_id.name.lower()}(
            ref self: ContractState,
            groth16_proof: Groth16ProofRaw,
            image_id: Span<u32>,
            journal_digest: Span<u32>,
            mpcheck_hint: MPCheckHint{curve_id.name},
            small_Q: E12DMulQuotient,
            msm_hint: Array<felt252>,
        ) -> bool {{
            // DO NOT EDIT THIS FUNCTION UNLESS YOU KNOW WHAT YOU ARE DOING.
            // ONLY EDIT THE process_public_inputs FUNCTION BELOW.
            groth16_proof.a.assert_on_curve({curve_id.value});
            groth16_proof.b.assert_on_curve({curve_id.value});
            groth16_proof.c.assert_on_curve({curve_id.value});

            let ic = ic.span();

            let claim_digest = compute_receipt_claim(image_id, journal_digest);

            // Start serialization with the hint array directly to avoid copying it.
            let mut msm_calldata: Array<felt252> = msm_hint;
            // Add the points from VK relative to the non-constant public inputs.
            Serde::serialize(@ic.slice(3, N_FREE_PUBLIC_INPUTS), ref msm_calldata);
            // Add the claim digest as scalars for the msm.
            msm_calldata.append(2);
            msm_calldata.append(claim_digest.low.into());
            msm_calldata.append(claim_digest.high.into());
            // Complete with the curve indentifier ({curve_id.value} for {curve_id.name}):
            msm_calldata.append({curve_id.value});

            // Call the multi scalar multiplication endpoint on the Garaga ECIP ops contract
            // to obtain claim0 * IC[3] + claim1 * IC[4].
            let mut _msm_result_serialized = core::starknet::syscalls::library_call_syscall(
                ECIP_OPS_CLASS_HASH.try_into().unwrap(),
                selector!("msm_g1_u128"),
                msm_calldata.span()
            )
                .unwrap_syscall();

            // Finalize vk_x computation by adding the precomputed T point.
            let vk_x = ec_safe_add(
                T, Serde::<G1Point>::deserialize(ref _msm_result_serialized).unwrap(), {curve_id.value}
            );

            // Perform the pairing check.
            let check = multi_pairing_check_{curve_id.name.lower()}_3P_2F_with_extra_miller_loop_result(
                G1G2Pair {{ p: vk_x, q: vk.gamma_g2 }},
                G1G2Pair {{ p: groth16_proof.c, q: vk.delta_g2 }},
                G1G2Pair {{ p: groth16_proof.a.negate({curve_id.value}), q: groth16_proof.b }},
                vk.alpha_beta_miller_loop_result,
                precomputed_lines.span(),
                mpcheck_hint,
                small_Q
            );
            if check == true {{
                self
                    .process_public_inputs(
                        starknet::get_caller_address(), claim_digest
                    );
                return true;
            }} else {{
                return false;
            }}
        }}
    }}
    #[generate_trait]
    impl InternalFunctions of InternalFunctionsTrait {{
        fn process_public_inputs(
            ref self: ContractState, user: ContractAddress, claim_digest: u256,
        ) {{ // Process the public inputs with respect to the caller address (user).
            // Update the storage, emit events, call other contracts, etc.
        }}
    }}
}}


    """

    create_directory(output_folder_path)
    src_dir = os.path.join(output_folder_path, "src")
    create_directory(src_dir)

    with open(os.path.join(src_dir, "groth16_verifier_constants.cairo"), "w") as f:
        f.write(constants_code)

    with open(os.path.join(src_dir, "groth16_verifier.cairo"), "w") as f:
        f.write(contract_code)

    with open(os.path.join(output_folder_path, "Scarb.toml"), "w") as f:
        f.write(get_scarb_toml_file("risc0_bn254_verifier", cli_mode=cli_mode))

    with open(os.path.join(src_dir, "lib.cairo"), "w") as f:
        f.write(
            f"""
mod groth16_verifier;
mod groth16_verifier_constants;
"""
        )
    subprocess.run(["scarb", "fmt"], check=True, cwd=output_folder_path)
    return constants_code


if __name__ == "__main__":
    pass

    RISCO_VK_PATH = (
        "hydra/garaga/starknet/groth16_contract_generator/examples/vk_risc0.json"
    )

    CONTRACTS_FOLDER = "src/contracts/"  # Do not change this

    FOLDER_NAME = "risc0_verifier"  # '_curve_id' is appended in the end.

    gen_risc0_groth16_verifier(
        RISCO_VK_PATH, CONTRACTS_FOLDER, FOLDER_NAME, ECIP_OPS_CLASS_HASH
    )
