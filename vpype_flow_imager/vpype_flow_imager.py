from collections import deque
import numpy as np
import cv2
from opensimplex import OpenSimplex
import tqdm
from .kdtree import get_nearest, add_point, make_kd_tree, rebalance

import click
import vpype as vp

import traceback
import ipdb
import sys

eps = 1e-10


def with_debugger(orig_fn):
    def new_fn(*args, **kwargs):
        try:
            return orig_fn(*args, **kwargs)
        except Exception as e:
            print(traceback.format_exc())
            print(e)
            ipdb.post_mortem()

    return new_fn


@click.command("flow_img")
@click.argument("filename", type=click.Path(exists=True))
@click.option(
    "-nc",
    "--noise_coeff",
    default=0.001,
    type=float,
    help="Simplex noise coordinate multiplier.  The smaller, the smoother the flow field.",
)
@vp.generator
@with_debugger
def vpype_flow_imager(filename, noise_coeff):
    """
    Insert documentation here...
    """
    gray_img = cv2.imread(filename, 0)
    numpy_paths = draw_image(gray_img, mult=noise_coeff)
    lc = vp.LineCollection()
    for path in numpy_paths:
        lc.append(path[:, 0] + path[:, 1] * 1.j)
    return lc


vpype_flow_imager.help_group = "Plugins"


def gen_flow_field(H, W, x_mult=1, y_mult=None):
    if y_mult is None:
        y_mult = x_mult
    x_noise = OpenSimplex(np.random.randint(9393931))
    y_noise = OpenSimplex(np.random.randint(9393931))
    field = np.zeros((H, W, 2), dtype=np.float64)
    for y in range(H):
        for x in range(W):
            x_val = x_noise.noise2d(x=x_mult * x, y=x_mult * y)
            y_val = y_noise.noise2d(x=y_mult * x, y=y_mult * y)
            norm = np.sqrt(x_val ** 2 + y_val ** 2)
            if norm > eps:
                x_val /= norm
                y_val /= norm
            else:
                x_val, y_val = 0, 0
            field[y, x, :] = (x_val, y_val)

    return field


def draw_image(gray_img, mult, max_sz=800):
    gray = resize_to_max(gray_img, max_sz)
    H, W = gray.shape

    field = gen_flow_field(H, W, x_mult=mult)
    fields = [VectorField(field),
              # VectorField(rotate_field(field, 60)),
              # VectorField(rotate_field(field, 120)),
              # VectorField(rotate_field(field, 180)),
              # VectorField(rotate_field(field, 240)),
              # VectorField(rotate_field(field, 300)),
              ]

    def d_sep_fn(pos):
        x, y = fit_inside(np.round(pos), gray).astype(np.int32)
        val = gray[y, x] / 255
        val = val**2
        return remap(val, 0, 1, 0.8, 10)

    paths = draw_fields_uniform(fields, d_sep_fn,
                                seedpoints_per_path=40,
                                guide=gray)
    return paths


class VectorField():
    def __init__(self, field_array):
        self.field = field_array
        self.shape = self.field.shape

    def __getitem__(self, pos):
        ''' pos should be (x, y) '''
        round_pos = np.round(pos[:2]).astype(np.int64)
        round_pos = fit_inside(round_pos, self.field)

        return self.field[round_pos[1], round_pos[0], :]


def fit_inside(xy, img):
    return np.clip(xy,
                   np.array([0, 0], xy.dtype),
                   np.array([img.shape[1] - 1, img.shape[0] - 1], xy.dtype))


def remap(x, src_min, src_max, dst_min, dst_max):
    x_01 = (x - src_min) / float(src_max - src_min)
    x_dst = x_01 * (dst_max - dst_min) + dst_min

    return x_dst


def dist_fn(x, y):
    return np.sum(np.square(x - y))


def draw_fields_uniform(fields, d_sep_fn, d_test_fn=None,
                        seedpoints_per_path=10,
                        guide=None,
                        max_length=20):
    if d_test_fn is None:
        def d_test_fn(*args, **kwargs):
            return d_sep_fn(*args, **kwargs) / 2

    H, W = fields[0].shape[:2]

    def should_stop(new_pos, kdtree, path, d_sep_fn):
        if not inside(np.round(new_pos), H, W):
            return True
        if kdtree is not None:
            point = new_pos.copy()
            nearest = get_nearest(kdtree, point, dim=2,
                                  dist_func=dist_fn,
                                  return_distances=True)
            dist, pt = nearest
            if dist < d_sep_fn(new_pos):
                return True
        # compute streamline length
        length = 0
        cur = path[0]

        for pt in path:
            length += np.linalg.norm(cur - pt)
            cur = pt

        if length > max_length:
            return True

        # look for loops
        # candidate = np.round(new_pos).astype(np.int64).reshape(1, 2)
        # for pt in reversed(path):
        #     if np.all(candidate == np.round(pt).astype(np.int64)):
        #         return True
        return False

    kdtree = make_kd_tree([np.array([-10, -10])], dim=2)
    paths = []
    rebalance_every = 500
    # save_every = 100
    seed_pos = np.array((W / 2, H / 2))
    seedpoints = [seed_pos]
    seedpoints = deque(seedpoints)
    pbar = tqdm.tqdm()
    try:
        while True:
            # try to find a suitable seedpoint in the queue
            try:
                while True:
                    seed_pos = seedpoints.pop()
                    if not inside(np.round(seed_pos), H, W):
                        continue

                    dist, _ = get_nearest(kdtree, seed_pos, dim=2,
                                          dist_func=dist_fn,
                                          return_distances=True)
                    if dist < d_sep_fn(seed_pos):
                        continue

                    break
            except IndexError:
                # no more seedpoints
                break

            start_field = np.random.randint(len(fields))

            def select_field(path_len, direction):
                same_field_len = 10

                idx = int(direction * path_len // same_field_len) + start_field
                idx = idx % len(fields)
                return fields[idx]

            class MemorySelector():
                def __init__(self, fields):
                    self.same_field_len = 10
                    self.cur_len = 0
                    self.idx = np.random.randint(len(fields))
                    self.fields = fields

                def select_field(self, path_len, direction):
                    if (path_len - self.cur_len) > self.same_field_len:
                        self.cur_len = path_len
                        idx_delta = np.random.randint(-1, 1 + 1)
                        self.idx = (self.idx + idx_delta) % len(self.fields)

                    return self.fields[self.idx]

            selector = MemorySelector(fields)

            path = compute_streamline(selector.select_field, seed_pos, kdtree,
                                      d_test_fn, d_sep_fn,
                                      should_stop_fn=should_stop)
            if len(path) <= 2:
                # nothing found
                # logging.debug('streamline ended immediately')
                continue

            for pt in path:
                add_point(kdtree, pt, dim=2)
            paths.append(np.array(path))
            if len(paths) % rebalance_every == 0:
                kdtree = rebalance(kdtree, dim=2)
            if len(paths) > 75:
                break
            # if len(paths) % save_every == 0:
            #     export_svg(paths, '/tmp/uniform_flow.svg')

            new_seedpoints = generate_seedpoints(path, d_sep_fn,
                                                 seedpoints_per_path)
            order = np.arange(len(new_seedpoints))
            np.random.shuffle(order)
            seedpoints.extend([new_seedpoints[i] for i in order])
            pbar.update(1)
    except KeyboardInterrupt:
        pass

    pbar.close()
    return paths


def inside(xy_pt, H, W):
    return (xy_pt[0] >= 0 and
            xy_pt[1] >= 0 and
            xy_pt[0] < W and
            xy_pt[1] < H)


def compute_streamline(field_getter, seed_pos, kdtree, d_test_fn, d_sep_fn,
                       should_stop_fn):
    direction_sign = 1  # first go with the field
    pos = seed_pos.copy()
    paths = []
    path = [pos.copy()]
    path_length = 0
    stop_tracking = False
    self_kdtree = make_kd_tree([(-20, -20)], dim=2)
    while True:
        field = field_getter(path_length, direction_sign)
        rk_force = runge_kutta(field, pos, d_test_fn(pos)) * direction_sign
        new_pos = pos + d_test_fn(pos) * rk_force

        # test validity
        if should_stop_fn(new_pos, kdtree, path, d_sep_fn):
            stop_tracking = True

        # prevent soft looping
        nearest_dist = get_nearest(self_kdtree, new_pos, dim=2,
                                   dist_func=dist_fn, return_distances=True)[0]
        if nearest_dist < d_sep_fn(pos):
            stop_tracking = True
        lookback = 15
        if len(path) >= 2 * lookback:
            add_point(self_kdtree, path[-lookback], dim=2)

        # fallback
        if len(path) >= 600:
            stop_tracking = True

        if not stop_tracking:
            path.append(new_pos.copy())
            path_length += np.linalg.norm(pos - new_pos)

        if stop_tracking:
            paths.append(path)
            if direction_sign == 1:
                # go to the other side from the seed
                direction_sign = -1
                pos = seed_pos.copy()
                path = [pos.copy()]
                path_length = 0
                # self_kdtree = make_kd_tree([(-20, -20)], dim=2)
                stop_tracking = False
            else:
                # both directions finished
                break
        else:
            pos = new_pos
    singleline = list(reversed(paths[1]))
    singleline.extend(paths[0])

    return singleline


def generate_seedpoints(path, d_sep_fn, N_seedpoints=10):
    # go along the path and create points perpendicular in d_sep distance
    seeds = []
    seedpoint_positions = np.linspace(0, len(path) - 1, N_seedpoints)
    seedpoint_ids = np.unique(np.round(seedpoint_positions)).tolist()

    cur_xy = path[0]
    direction = path[1] - path[0]
    direction /= max(np.linalg.norm(direction), eps)
    normal = np.array((direction[1], -direction[0]))
    margin = 1.1
    seeds.append(cur_xy + margin * d_sep_fn(cur_xy) * normal)
    seeds.append(cur_xy - margin * d_sep_fn(cur_xy) * normal)

    for i in range(1, len(path)):
        if i not in seedpoint_ids:
            continue
        last_xy = cur_xy.copy()
        cur_xy = path[i]
        direction = cur_xy - last_xy
        direction /= max(np.linalg.norm(direction), eps)
        normal = np.array((direction[1], -direction[0]))
        seeds.append(cur_xy + margin * d_sep_fn(cur_xy) * normal)
        seeds.append(cur_xy - margin * d_sep_fn(cur_xy) * normal)

    return seeds


def runge_kutta(field, pos, h):
    k1 = field[pos]

    k2_pos = pos + (h / 2) * k1
    k2 = field[k2_pos]

    k3_pos = pos + (h / 2) * k2
    k3 = field[k3_pos]

    k4_pos = pos + h * k3
    k4 = field[k4_pos]

    # Runge-Kutta for the win
    rk = (1 / 6) * (k1 + 2 * k2 + 2 * k3 + k4)
    return rk


def resize_to_max(img, max_sz):
    H_scale = max_sz / img.shape[0]
    W_scale = max_sz / img.shape[1]

    scale = min(H_scale, W_scale)
    return cv2.resize(img, None, fx=scale, fy=scale)

