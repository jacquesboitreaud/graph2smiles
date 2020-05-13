import os
import sys

script_dir = os.path.dirname(os.path.realpath(__file__))
if __name__ == '__main__':
    sys.path.append(os.path.join(script_dir, '..', '..'))

import argparse

from cbas.gen_prob import GenProb
from utils import *
from model import model_from_json

import pickle


def get_samples(prior_model, search_model, max):
    sample_selfies = []
    weights = []
    sample_selfies_set = set()
    tries = 0
    # TODO : RESET
    stop = 5
    batch_size = 100

    # Importance weights
    while tries < stop and len(sample_selfies) < max:
        new_ones = 0

        # Get raw samples
        samples_z = search_model.sample_z_prior(n_mols=batch_size)
        gen_seq = search_model.decode(samples_z)
        _, sample_indices = torch.max(gen_seq, dim=1)

        # Compute weights while we have indices and store them: p(x|z, theta)/p(x|z, phi)
        batch_weights = GenProb(sample_indices, samples_z, prior_model) / \
                        GenProb(sample_indices, samples_z, search_model)

        # Check the novelty
        batch_selfies = search_model.indices_to_smiles(sample_indices)
        for i, s in enumerate(batch_selfies):
            new_selfie = decoder(s)
            if new_selfie not in sample_selfies_set:
                new_ones += 1
                sample_selfies_set.add(new_selfie)
                sample_selfies.append(new_selfie)
                weights.append(batch_weights[i])
        tries += 1

        print(f'{new_ones}/{batch_size} unique smiles sampled')
    return sample_selfies, weights


if __name__ == '__main__':
    pass

    parser = argparse.ArgumentParser()

    parser.add_argument('--prior_name', type=str, default='inference_default')  # the prior VAE (pretrained)
    parser.add_argument('--search_name', type=str, default='search_vae')  # the prior VAE (pretrained)
    parser.add_argument('--max', type=int, default=1000)  # the prior VAE (pretrained)

    # =======

    args, _ = parser.parse_known_args()
    prior_model = model_from_json(args.prior_name)

    # We start by creating another prior instance, then replace it with the actual weights
    # name = search_vae
    search_model = model_from_json(args.prior_name)
    name = args.search_name
    model_weights_path = os.path.join(script_dir, 'results', 'models', name, 'weights.pth')
    search_model.load(model_weights_path)

    samples, weights = get_samples(prior_model, search_model, max=args.max)

    # Memoization, we split the list into already docked ones and dump a simili-docking csv
    whole_path = os.path.join(script_dir, '..', '..', 'data', 'drd3_scores.pickle.p')
    docking_whole_results = pickle.load(open(whole_path, 'rb'))

    filtered_smiles = list()
    already_smiles = list()
    already_scores = list()
    for i, smile in enumerate(samples):
        if smile in docking_whole_results:
            already_smiles.append(smile)
            already_scores.append(docking_whole_results[smile])
        else:
            filtered_smiles.append(smile)

    # Dump simili-docking
    dump_path = os.path.join(script_dir, 'results', 'docking_small_results', 'simili.csv')
    df = pd.DataFrame.from_dict({'smile': already_smiles, 'score': already_scores})
    df.to_csv(dump_path)

    # Dump for the docker
    dump_path = os.path.join(script_dir, 'results', 'docker_samples.p')
    pickle.dump(filtered_smiles, open(dump_path, 'wb'))

    # Dump for the trainer
    dump_path = os.path.join(script_dir, 'results', 'samples.p')
    pickle.dump(samples, open(dump_path, 'wb'))
